import hmac
import logging
import re
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

import storage
from adjustments import get_total_adjustment
from config import (
    APPROVER_PHONE_NUMBER,
    CAFE_NAME,
    DELIVERY_DAYS,
    EMPLOYEE_PHONE_NUMBERS,
    MIN_DAYS_BETWEEN_ORDERS,
    WAIT_FOR_COUNT_HOURS,
    STOCK_TARGETS,
    STALE_THRESHOLD_DAYS,
    SUPPLIER_PHONE_NUMBER,
    TRIGGER_KEY,
    WAIT_FOR_APPROVAL_HOURS,
)
from models import Approval, JobRun, OrderLine, StockLevel
from order_calculator import calculate_order, format_order_lines, format_order_message
from sms import send_sms, validate_twilio_request
from stock_parser import format_confirmation, parse_stock_sms

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")

# What a run of the weekly order job ended in — written to the job_runs log.
OUTCOME_ORDERED = "ordered"
OUTCOME_NO_ORDER_NEEDED = "no_order_needed"
OUTCOME_STALE = "stale"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
OUTCOME_SEND_UNCONFIRMED = "send_unconfirmed"
# An order went out so recently that its delivery cannot have been counted: nothing ordered.
OUTCOME_RECENT_ORDER = "recent_order"
# The order waited a day for a stock count that never came: staff told to order by hand.
OUTCOME_CLOSED = "closed_no_count"
# The order is worked out and with the approver. Nothing has gone to the supplier.
OUTCOME_AWAITING_APPROVAL = "awaiting_approval"
# The approver said no, or did not answer in time: nothing sent, staff told to order by hand.
OUTCOME_NOT_APPROVED = "closed_not_approved"
ALL_OUTCOMES = (
    OUTCOME_ORDERED,
    OUTCOME_NO_ORDER_NEEDED,
    OUTCOME_STALE,
    OUTCOME_SKIPPED,
    OUTCOME_FAILED,
    OUTCOME_SEND_UNCONFIRMED,
    OUTCOME_RECENT_ORDER,
    OUTCOME_CLOSED,
    OUTCOME_AWAITING_APPROVAL,
    OUTCOME_NOT_APPROVED,
)
# Outcomes after which the job must not order again in the same week without force.
HANDLED_OUTCOMES = (
    OUTCOME_ORDERED,
    OUTCOME_NO_ORDER_NEEDED,
    OUTCOME_SEND_UNCONFIRMED,
    OUTCOME_RECENT_ORDER,
    OUTCOME_CLOSED,
    OUTCOME_AWAITING_APPROVAL,
    OUTCOME_NOT_APPROVED,
)
# The 1pm reminder for a count the order is waiting on. Logged, but deliberately not in
# ALL_OUTCOMES: it is not a run of the order job, and the week must still read as waiting.
OUTCOME_CHASED = "chased"

ORDER_WEEKDAY = 2  # Wednesday (Monday is 0) — matches the scheduler below
FORCE_COOLDOWN = timedelta(minutes=10)
DELIVERY_TIME = timedelta(days=DELIVERY_DAYS)
WAIT_FOR_COUNT = timedelta(hours=WAIT_FOR_COUNT_HOURS)
WAIT_FOR_APPROVAL = timedelta(hours=WAIT_FOR_APPROVAL_HOURS)
REMIND_AFTER = timedelta(hours=4)  # one reminder, this long after a count is asked for
COUNT_FORMAT = "Almond: X, Oat: X, Soy: X, LF: X bottles, Coconut: X bottles"

_order_lock = threading.Lock()


class SupplierSendUnconfirmed(Exception):
    """The supplier SMS raised, so it is unknown whether the supplier received the order."""

app = Flask(__name__)


@app.before_request
def init_database():
    """Ensure DB tables exist on first request."""
    if not getattr(app, "_db_initialized", False):
        storage.init_db()
        app._db_initialized = True


@app.route("/sms", methods=["POST"])
def incoming_sms():
    """Twilio webhook for incoming SMS from employees."""
    # Validate Twilio signature — use the original URL from X-Forwarded headers
    # (ngrok/reverse proxies rewrite the URL, breaking signature validation)
    signature = request.headers.get("X-Twilio-Signature", "")
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    original_url = f"{proto}://{host}{request.path}"
    if not validate_twilio_request(original_url, request.form.to_dict(), signature):
        logger.warning("Invalid Twilio signature — rejecting request")
        return "Forbidden", 403

    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    # The approver's yes or no to an order that is waiting. Read before the staff path,
    # and never by the model: only a stock count goes to parse_stock_sms.
    if APPROVER_PHONE_NUMBER and from_number == APPROVER_PHONE_NUMBER:
        answer = _answer_from_approver(body)
        if answer is not None:
            resp = MessagingResponse()
            resp.message(answer)
            return str(resp)

    # Check employee allowlist
    if from_number not in EMPLOYEE_PHONE_NUMBERS:
        logger.warning("SMS from unknown number: %s", from_number)
        resp = MessagingResponse()
        resp.message("Sorry, you are not authorised to report stock levels.")
        return str(resp)

    if not body:
        resp = MessagingResponse()
        resp.message("Please send your current stock levels.")
        return str(resp)

    # Parse the stock message with Claude
    try:
        parsed = parse_stock_sms(body)
    except Exception as e:
        logger.error("Failed to parse stock SMS: %s", e)
        resp = MessagingResponse()
        resp.message(f"Could not parse stock levels. Please try again with format:\n{COUNT_FORMAT}")
        return str(resp)

    # Save to database
    storage.save_stock_report(from_number, body, parsed)
    logger.info("Stock report saved from %s: %s", from_number, parsed)

    # If the order job is waiting on a count, either place the order now or say what is
    # still missing — staff were told the order goes out once the count is in.
    # In a thread: the job makes several API calls and Twilio's webhook times out at 15s.
    confirmation = format_confirmation(parsed)
    try:
        still_needed = _still_needed(_melbourne_today())
        if still_needed:
            confirmation += (
                f"\nStill need a count for: {', '.join(still_needed)} "
                "before this week's order can go."
            )
        elif still_needed is not None:
            threading.Thread(target=run_weekly_order, daemon=True).start()
            if APPROVER_PHONE_NUMBER:
                confirmation += "\nThat completes the count. This week's order is going for approval now."
            else:
                confirmation += "\nThat completes the count — placing this week's order now."
    except Exception:
        logger.exception("Could not check whether the order job is waiting on this count")

    # Send confirmation
    resp = MessagingResponse()
    resp.message(confirmation)
    return str(resp)


def _melbourne_today() -> date:
    """Today's date at the cafe. date.today() is the host's date, and on a UTC host
    9am Wednesday in Melbourne is still Tuesday."""
    return datetime.now(MELBOURNE_TZ).date()


def _order_week(day: date) -> date:
    """The Wednesday whose order a given day belongs to (that day, or the Wednesday before it).
    An order released late on a Friday still belongs to that week's Wednesday, so it
    never counts against the following Wednesday's order."""
    return day - timedelta(days=(day.weekday() - ORDER_WEEKDAY) % 7)


def _stale_items(
    current_stock: dict[str, StockLevel],
    as_of: datetime,
    delivered_by: datetime | None = None,
) -> list[str]:
    """Labels of items with no count, or a count older than the staleness threshold at `as_of`.
    Checked per item: one fresh item must not make last month's count of another look current.
    A count taken before `delivered_by` (when the last order's delivery is due) is stale
    however recent it is: it cannot include that delivery, so ordering from it sends the
    supplier the same shortfall twice."""
    cutoff = as_of - timedelta(days=STALE_THRESHOLD_DAYS)
    stale = []
    for item_key, target in STOCK_TARGETS.items():
        level = current_stock.get(item_key)
        if level is None or level.reported_at is None or level.reported_at < cutoff:
            stale.append(target["label"])
        elif delivered_by is not None and level.reported_at < delivered_by:
            stale.append(target["label"])
    return stale


def _last_order_run() -> JobRun | None:
    """The last run that sent the supplier an order, or may have. A send that could not be
    confirmed counts: it is unknown whether it arrived, and staff were told to check."""
    return storage.get_last_run((OUTCOME_ORDERED, OUTCOME_SEND_UNCONFIRMED))


def _delivered_by(last_order: JobRun | None) -> datetime | None:
    """When the last order's delivery is due. Counts taken before this can't include it."""
    return last_order.at + DELIVERY_TIME if last_order else None


def _order_reference(last_order: JobRun) -> str:
    """How to describe the last order to staff — true whether or not the send was confirmed."""
    sent_on = f"{last_order.run_date:%A} {last_order.run_date.day}/{last_order.run_date.month}"
    if last_order.outcome == OUTCOME_SEND_UNCONFIRMED:
        return f"An order was sent on {sent_on} that may not have reached the supplier"
    return f"An order went to the supplier on {sent_on}"


def _already_handled(today: date) -> bool:
    """True if this week's order was already sent (or may have been), or found not to be needed."""
    last_run = storage.get_last_run(HANDLED_OUTCOMES)
    if last_run and _order_week(last_run.run_date) == _order_week(today):
        return True
    # Orders sent before the run log existed are only in order_history, some of them
    # dated a day early by the old host-date bug — so those are judged by days, not by week.
    if last_run is None:
        history = storage.get_order_history(1)
        if history:
            last_order = date.fromisoformat(history[0]["order_date"])
            return (today - last_order).days < MIN_DAYS_BETWEEN_ORDERS
    return False


def _open_request(today: date) -> JobRun | None:
    """This week's run that stopped to ask staff for a stock count, if nothing has run since
    (an order, a closure, any other run of the job ends the request)."""
    last_run = storage.get_last_run(ALL_OUTCOMES)
    if last_run and last_run.outcome == OUTCOME_STALE and _order_week(last_run.run_date) == _order_week(today):
        return last_run
    return None


def _waiting_run(today: date) -> JobRun | None:
    """The open request for a count, while a count can still release the order. After
    WAIT_FOR_COUNT the wait is over even if the closing text was never sent (process down
    at the time): a count texted days later must only record stock, never place an order."""
    request = _open_request(today)
    if request and datetime.now(timezone.utc) - request.at < WAIT_FOR_COUNT:
        return request
    return None


def _freshness_as_of(today: date) -> datetime:
    """The moment stock counts are judged against. Normally now. While an order is waiting
    on a count, it is when the job first asked — otherwise the counts that were fresh on
    Wednesday morning go stale while staff find the missing one, and the order never releases."""
    waiting = _waiting_run(today)
    return waiting.at if waiting else datetime.now(timezone.utc)


def _still_needed(today: date) -> list[str] | None:
    """None if no order is waiting on a stock count. Otherwise the items it still needs —
    an empty list means the count is complete and the order can go."""
    waiting = _waiting_run(today)
    if waiting is None:
        return None
    return _stale_items(
        storage.get_current_stock(), waiting.at, _delivered_by(_last_order_run())
    )


def _notify_employees(body: str) -> int:
    """Text every employee. One bad number must not stop the others getting the message.
    Returns how many sends succeeded, so a caller can log a text that reached nobody."""
    sent = 0
    for phone in EMPLOYEE_PHONE_NUMBERS:
        try:
            send_sms(phone, body)
            sent += 1
        except Exception:
            logger.exception("Failed to send SMS to employee %s", phone)
    return sent


def build_employee_confirmation(
    order_lines: list[OrderLine],
    current_stock: dict[str, StockLevel],
    today: date,
) -> str:
    """The summary staff get after the order is sent: what was ordered, and what
    they'll have once it arrives. Each item is shown in the unit staff count it in."""
    date_str = f"{today.day}/{today.month}/{str(today.year)[2:]}"
    order_by_item = {ol.item: ol.quantity for ol in order_lines}

    conf_lines = [date_str, "", "Stock ordered:"]
    for item_key, target in STOCK_TARGETS.items():
        short_label = target["label"].replace(" Milk", "")
        conf_lines.append(f"{short_label} * {order_by_item.get(item_key, 0)}")

    conf_lines.extend(["", "Total Inventory until next Wednesday:"])
    for item_key, target in STOCK_TARGETS.items():
        short_label = target["label"].replace(" Milk", "")
        bottles_per_box = target["bottles_per_box"]
        ordered_boxes = order_by_item.get(item_key, 0)
        current = current_stock.get(item_key)

        current_bottles = 0.0
        if current:
            current_bottles = (
                current.quantity * bottles_per_box if current.unit == "boxes" else current.quantity
            )
        total_bottles = current_bottles + ordered_boxes * bottles_per_box

        if target["unit"] == "boxes":
            conf_lines.append(f"{short_label} * {total_bottles / bottles_per_box:g}")
        else:
            conf_lines.append(f"{short_label} * {total_bottles:g} bottles")

    return "\n".join(conf_lines)


def run_weekly_order(force: bool = False) -> str:
    """Scheduled job: calculate and send weekly order to supplier.
    Returns the outcome (one of the OUTCOME_* values) for the caller to report."""
    logger.info("Running weekly order job...")
    today = _melbourne_today()

    # One run at a time: the scheduler, the manual trigger and a late stock count can
    # overlap, and the second run must see the first one's result before it decides.
    with _order_lock:
        outcome = _run_once(today, force)
        if outcome == OUTCOME_STALE:
            try:
                complete = _still_needed(today) == []
            except Exception:
                logger.exception("Could not re-check the count after asking for it")
                complete = False
            if complete:
                # The count landed while this run was asking for it: the text got "Stock
                # updated" (no run was waiting yet), and this run then logged "stale". Left
                # there, the count is complete, nothing is missing to chase, and no order
                # ever goes. So place it now.
                logger.warning("The count arrived while it was being asked for — placing the order")
                outcome = _run_once(today, False)
            else:
                _schedule_follow_ups()
    return outcome


def _run_once(today: date, force: bool) -> str:
    """One attempt at the order job, with its failure handling and its run-log row."""
    try:
        outcome, detail = _place_order(today, force)
    except SupplierSendUnconfirmed as e:
        # The send raised, which does not prove the supplier got nothing (a timeout
        # after Twilio accepted the message looks the same). Counts as handled, so
        # nothing re-sends on its own; staff check before anyone orders again.
        logger.exception("Supplier SMS could not be confirmed")
        outcome, detail = OUTCOME_SEND_UNCONFIRMED, repr(e.__cause__)
        _notify_employees(
            "The automatic milk order may not have reached the supplier. "
            "Please check with them before ordering again."
        )
    except Exception as e:
        # Raised before the supplier SMS was attempted, so nothing reached the supplier.
        # Staff need to know so the order still happens this week.
        logger.exception("Weekly order job failed")
        outcome, detail = OUTCOME_FAILED, repr(e)
        _notify_employees(
            "The automatic milk order failed today and nothing was sent to the supplier. "
            "Please place this week's order manually."
        )

    try:
        storage.log_run(today, outcome, detail)
    except Exception:
        logger.exception("Could not write the run log (outcome: %s)", outcome)
    return outcome


def _place_order(today: date, force: bool) -> tuple[str, str]:
    """Do one run of the order job. Returns (outcome, detail for the run log)."""
    storage.init_db()

    handled = _already_handled(today)
    if not force and handled:
        logger.warning("This week's order was already handled — not sending again")
        return OUTCOME_SKIPPED, "already handled this week"
    if force and APPROVER_PHONE_NUMBER and storage.get_pending_approval():
        # The approver already has this week's order. A second request would leave two
        # orders a yes could mean, and a forced run must not go around their answer.
        logger.warning("Forced run refused — an order is waiting for approval")
        return OUTCOME_SKIPPED, "forced run refused: an order is waiting for approval"
    if force:
        # force is for a deliberate re-send, not for a double-click or a browser retry.
        last_sent = storage.get_last_run((OUTCOME_ORDERED,))
        if last_sent and datetime.now(timezone.utc) - last_sent.at < FORCE_COOLDOWN:
            logger.warning("Forced run refused — an order went out moments ago")
            return OUTCOME_SKIPPED, "forced run refused: an order was sent minutes ago"

    # A forced run is a deliberate re-send of the same order, so it is the one case
    # allowed to ignore the last order: it may reuse the counts that order was made from.
    last_order = None if force else _last_order_run()
    delivered_by = _delivered_by(last_order)

    if delivered_by and datetime.now(timezone.utc) < delivered_by:
        # The last order's delivery is not due yet, so no count can include it — not the
        # one on file, and not one staff send today. Ordering now doubles the shortfall;
        # asking for a count would do the same a few hours later. So: no order this run.
        logger.warning("An order went out %s — its delivery is not due yet, not ordering", last_order.run_date)
        _notify_employees(
            f"{_order_reference(last_order)}, so today's automatic milk order is skipped to "
            "avoid doubling up. Check stock once that delivery is in — if you'll run short "
            "before next Wednesday, please order by hand."
        )
        return OUTCOME_RECENT_ORDER, f"order of {last_order.run_date} not yet delivered"

    # Check every item has a fresh count, taken since the last delivery was due.
    current_stock = storage.get_current_stock()
    as_of = _freshness_as_of(today)
    stale = _stale_items(current_stock, as_of, delivered_by)
    if stale:
        if force and handled:
            # A forced re-send in a week that is already handled must not reopen it as
            # "waiting on a count": staff would be told the order has not gone when it has.
            logger.warning("Forced run refused — no fresh count for %s", stale)
            return OUTCOME_SKIPPED, f"forced run refused: no fresh count for {', '.join(stale)}"
        logger.warning("Stock data is stale or missing for %s — requesting update", stale)
        deadline = (
            f"If it isn't in within {WAIT_FOR_COUNT_HOURS} hours, please order by hand.\n"
            f"Format: {COUNT_FORMAT}"
        )
        if last_order and not _stale_items(current_stock, as_of):
            # Every count is recent; they are only stale because they predate the delivery.
            # "Once that delivery has arrived" matters: DELIVERY_DAYS is an estimate, and a
            # recount of the same shelf before the milk turns up orders it all again.
            request = (
                f"Hi! {_order_reference(last_order)}, and the last stock count was taken "
                "before that delivery was due. Please count again once that delivery has "
                "arrived and send it — this week's order will go out as soon as the new "
                f"count arrives. {deadline}"
            )
        else:
            request = (
                "Hi! It's ordering day but we don't have a recent stock count for: "
                f"{', '.join(stale)}. Please send current stock levels ASAP — "
                f"the order will go out as soon as the count is in. {deadline}"
            )
        _notify_employees(request)
        return OUTCOME_STALE, f"no fresh count for {', '.join(stale)}"

    # The order is arithmetic: targets minus the counted stock, in whole boxes. No model
    # decides a quantity — nobody checks the order before the supplier gets it.
    order_lines = calculate_order(current_stock, today)
    detail = "sent: " + (
        ", ".join(f"{ol.item}={ol.quantity}" for ol in order_lines) or "nothing"
    )

    if not order_lines:
        logger.info("All stock levels are sufficient — no order needed this week")
        _notify_employees("Stock levels are good — no order needed this week.")
        return OUTCOME_NO_ORDER_NEEDED, detail

    if APPROVER_PHONE_NUMBER:
        # On trial: the order goes to the approver, and only their yes sends it on.
        _ask_for_approval(today, order_lines, current_stock)
        return OUTCOME_AWAITING_APPROVAL, "awaiting approval: " + detail.removeprefix("sent: ")

    # Format and send order to supplier
    adjustment = get_total_adjustment(today)
    order_message = format_order_message(order_lines, today, adjustment)

    try:
        send_sms(SUPPLIER_PHONE_NUMBER, order_message)
    except Exception as e:
        raise SupplierSendUnconfirmed(detail) from e
    logger.info("Order sent to supplier: %s", order_message)

    _record_sent_order(today, order_lines, current_stock)
    return OUTCOME_ORDERED, detail


def _record_sent_order(
    today: date, order_lines: list[OrderLine], current_stock: dict[str, StockLevel]
) -> None:
    """After the supplier has the order: save it and tell staff. Nothing here may raise,
    or the caller would report an order that went out as one that did not."""
    try:
        storage.save_order(today, order_lines)
    except Exception:
        logger.exception("Order was sent but could not be saved to order history")
    try:
        _notify_employees(build_employee_confirmation(order_lines, current_stock, today))
    except Exception:
        logger.exception("Order was sent but the staff confirmation could not be built")


def _clock_text(moment: datetime) -> str:
    """A moment in the cafe's time, as staff would say it: e.g. '9:00am Thursday'."""
    local = moment.astimezone(MELBOURNE_TZ)
    return f"{local.hour % 12 or 12}:{local.minute:02d}{'am' if local.hour < 12 else 'pm'} {local:%A}"


def _count_text(level: StockLevel | None) -> str:
    """A stock count as staff sent it: '8 boxes', '1 bottle'."""
    if level is None:
        return "no count"
    unit = {"boxes": "box", "bottles": "bottle"}[level.unit] if level.quantity == 1 else level.unit
    return f"{level.quantity:g} {unit}"


def build_approval_request(approval: Approval) -> str:
    """The text the approver answers: the order as the supplier would get it, the counts it
    was worked out from (a misread count is the mistake they can catch), and the deadline."""
    counted = ", ".join(
        f"{target['label'].replace(' Milk', '')} {_count_text(approval.counts.get(item_key))}"
        for item_key, target in STOCK_TARGETS.items()
    )
    return "\n".join([
        f"{CAFE_NAME} milk order for approval:",
        "",
        *format_order_lines(approval.order_lines),
        "",
        f"Counted: {counted}",
        "",
        "Reply YES to send it to the supplier, or NO to stop it.",
        f"If there's no reply by {_clock_text(approval.requested_at + WAIT_FOR_APPROVAL)}, it won't be sent.",
    ])


def _ask_for_approval(
    today: date, order_lines: list[OrderLine], current_stock: dict[str, StockLevel]
) -> None:
    """Hold the order and text it to the approver. Nothing goes to the supplier here."""
    approval = storage.save_approval_request(today, order_lines, current_stock)
    try:
        send_sms(APPROVER_PHONE_NUMBER, build_approval_request(approval))
    except Exception:
        # The approver may never have seen it, so no reply may release it. The run then
        # fails the ordinary way: staff are told nothing was sent and to order by hand.
        storage.decide_approval(approval.id, storage.APPROVAL_CANCELLED)
        raise
    logger.info("Order held for approval (request %s)", approval.id)
    _notify_employees(
        "This week's milk order is worked out and waiting for approval before it goes to "
        "the supplier. You'll get a text when it's sent."
    )


# How the approver's reply is read. A fixed list, not a model: whether an order goes must
# not rest on a guess. Anything the list does not settle is asked again.
_YES_OPENERS = {"yes", "y", "yep", "yeah", "yup", "ya", "ok", "okay", "approve", "approved", "confirm", "confirmed"}
# Looser ways to say yes count only when they are the whole reply: "send it" is a yes,
# "send me the count again" is not.
_YES_WHOLE_REPLIES = {"send", "send it", "go", "go ahead", "sure", "do it", "k"}
_NO_OPENERS = {"no", "n", "nope", "nah", "not", "dont", "stop", "cancel", "reject", "rejected", "wrong"}
# A yes followed by any of these is not a plain yes ("yes but make the oat 4").
_DOUBT_WORDS = _NO_OPENERS | {"but", "wait", "hold", "except", "change", "instead", "unless", "if"}
_THUMBS_UP, _THUMBS_DOWN = "\U0001F44D", "\U0001F44E"


def _read_approval_reply(body: str) -> bool | None:
    """True for a yes, False for a no, None when it is neither or it is unclear. Leans
    towards no: a wrong no costs a phone call, a wrong yes costs an order."""
    text = body.strip().lower().replace("'", "").replace("\u2019", "")
    words = re.findall(r"[a-z]+", text)
    if not words:
        if text.startswith(_THUMBS_UP):
            return True
        return False if text.startswith(_THUMBS_DOWN) else None
    if words[0] in _NO_OPENERS:
        return False
    if " ".join(words) in _YES_WHOLE_REPLIES:
        return True
    if words[0] in _YES_OPENERS and not _DOUBT_WORDS.intersection(words[1:]):
        return True
    return None


NOTHING_WAITING = "There's no milk order waiting for approval. Nothing was sent just now."


def _answer_from_approver(body: str) -> str | None:
    """Deal with a text from the approver and return the reply to send them. Returns None
    when the text is not an answer to anything — an approver who also counts stock, texting
    a count while no order is waiting — so the caller reads it as a staff text."""
    decision = _read_approval_reply(body)
    # Under the order lock: the weekly run, a second yes and the approver's answer can
    # overlap, and only one of them may act on the waiting order.
    with _order_lock:
        approval = storage.get_pending_approval()
        if approval is None:
            if decision is None and APPROVER_PHONE_NUMBER in EMPLOYEE_PHONE_NUMBERS:
                return None
            return NOTHING_WAITING
        if datetime.now(timezone.utc) - approval.requested_at >= WAIT_FOR_APPROVAL:
            # The wait is over even if the closing text never ran (process down at the time).
            return _close_unanswered(approval)
        if decision is None:
            return "Reply YES to send this week's milk order to the supplier, or NO to stop it."
        if decision:
            return _send_approved_order(approval, body)
        return _turn_down_order(approval, body)


def _log_run(today: date, outcome: str, detail: str) -> None:
    try:
        storage.log_run(today, outcome, detail)
    except Exception:
        logger.exception("Could not write the run log (outcome: %s)", outcome)


def _send_approved_order(approval: Approval, reply: str) -> str:
    """The approver said yes: send the supplier the order they were shown. Runs inside the
    webhook request rather than a thread, so the reply can say what actually happened."""
    today = _melbourne_today()
    detail = "approved; sent: " + ", ".join(f"{ol.item}={ol.quantity}" for ol in approval.order_lines)
    try:
        order_message = format_order_message(approval.order_lines, today, get_total_adjustment(today))
        # Claimed before the send: a second yes finds nothing waiting and sends nothing.
        if not storage.decide_approval(approval.id, storage.APPROVAL_APPROVED, reply):
            return NOTHING_WAITING
    except Exception:
        logger.exception("The approved order could not be prepared — nothing was sent")
        return (
            "Something went wrong and the order was NOT sent to the supplier. "
            "Reply YES to try again, or place the order by hand."
        )

    try:
        send_sms(SUPPLIER_PHONE_NUMBER, order_message)
    except Exception as e:
        # As in _run_once: the send raised, which does not prove the supplier got nothing.
        logger.exception("Supplier SMS could not be confirmed")
        _log_run(today, OUTCOME_SEND_UNCONFIRMED, repr(e))
        _notify_employees(
            "The automatic milk order may not have reached the supplier. "
            "Please check with them before ordering again."
        )
        return "The order may not have reached the supplier. Please check with them before ordering again."
    logger.info("Order sent to supplier after approval: %s", order_message)

    _record_sent_order(today, approval.order_lines, approval.counts)
    _log_run(today, OUTCOME_ORDERED, detail)
    return "Sent to the supplier. Staff have been told."


def _turn_down_order(approval: Approval, reply: str) -> str:
    """The approver said no: nothing goes to the supplier and the week is closed. Their
    words are kept with the approval — the only record of why an order was turned down."""
    if not storage.decide_approval(approval.id, storage.APPROVAL_REJECTED, reply):
        return NOTHING_WAITING
    logger.warning("The approver turned down this week's order — nothing sent")
    reached = _notify_employees(
        "This week's milk order was not approved, so it has NOT been sent to the supplier. "
        "Please order by hand. The next automatic order is next Wednesday."
    )
    # Dated to the day the approver was asked: this closes that order's week, and must
    # not count against a Wednesday that has started since.
    _log_run(
        approval.order_date,
        OUTCOME_NOT_APPROVED,
        f"approver said no; told {reached} of {len(EMPLOYEE_PHONE_NUMBERS)} staff",
    )
    return "OK, nothing was sent to the supplier. Staff have been told to order by hand."


def _close_unanswered(approval: Approval) -> str:
    """The wait for an answer is over: nothing goes to the supplier, whatever comes now."""
    if not storage.decide_approval(approval.id, storage.APPROVAL_EXPIRED):
        return NOTHING_WAITING
    logger.warning("No answer from the approver in time — this week's order was not sent")
    reached = _notify_employees(
        "This week's milk order was not approved in time, so it has NOT been sent to the "
        "supplier. Please order by hand. The next automatic order is next Wednesday."
    )
    # Dated to the day the approver was asked: this closes that order's week, and must
    # not count against a Wednesday that has started since.
    _log_run(
        approval.order_date,
        OUTCOME_NOT_APPROVED,
        f"no answer from the approver; told {reached} of {len(EMPLOYEE_PHONE_NUMBERS)} staff",
    )
    return (
        "That order was not approved in time, so it was not sent to the supplier. "
        "Please place this week's order by hand."
    )


def _deadline_text(request: JobRun) -> str:
    """When the wait for a count ends, in the cafe's time: e.g. '9:00am Thursday'."""
    return _clock_text(request.at + WAIT_FOR_COUNT)


def chase_missing_count(final: bool = False) -> list[str]:
    """Follow-up on a request for a stock count, scheduled when the request is made
    (_schedule_follow_ups). The request is one text and is easy to miss; without this the
    week ends with no order and nobody told.

    The first follow-up (REMIND_AFTER the request) reminds staff once. The `final` one
    (WAIT_FOR_COUNT after it) closes the week: it says the order was not placed and to order
    by hand, and from then on a count only records stock. Returns the items still missing."""
    today = _melbourne_today()
    release = False
    with _order_lock:
        try:
            request = _open_request(today)
            if request is None:
                return []
            waiting = _waiting_run(today) is not None
            missing = _stale_items(
                storage.get_current_stock(), request.at, _delivered_by(_last_order_run())
            )
            names = ", ".join(missing)
            everyone = len(EMPLOYEE_PHONE_NUMBERS)

            if not final and not waiting:
                return []  # too late for a reminder; the closing follow-up ends the week
            if final and waiting:
                # This closing job was booked for an earlier request; the one open now has
                # not had its day yet. Closing it would cut the promised wait short.
                logger.warning("Closing follow-up ran before the open request's deadline — rebooking")
                _schedule_follow_ups()
                return []
            if not final and not missing:
                # The count is complete but the request is still open, so the order never
                # went: the thread releasing it died (a restart), or it never started.
                release = True
            elif not final:
                reminded = storage.get_last_run((OUTCOME_CHASED,))
                if reminded and reminded.at > request.at:
                    return []  # already reminded for this request — a re-run must not text twice
                logger.warning("Order still waiting on a count for %s — reminding staff", missing)
                reached = _notify_employees(
                    f"Reminder: still waiting on a stock count for: {names}. This week's milk "
                    "order has not gone out yet — it goes as soon as the count is in. If it "
                    f"isn't in by {_deadline_text(request)}, please order by hand.\n"
                    f"Format: {COUNT_FORMAT}"
                )
                storage.log_run(today, OUTCOME_CHASED, f"reminder for {names}; reached {reached} of {everyone} staff")
                if reached == 0:
                    logger.error("The stock count reminder reached no staff number")
            else:
                # Closing always ends the week with a text, whatever state the count is in:
                # an open request that reaches its deadline means no order went out.
                logger.warning("Closing this week's order — no order was placed (missing: %s)", names or "nothing")
                reason = f"No stock count came in for: {names}, so this" if missing else "This"
                reached = _notify_employees(
                    f"{reason} week's milk order has NOT been placed and the automatic order "
                    "is closed for this week. Please order from the supplier by hand. The next "
                    "automatic order is next Wednesday."
                )
                storage.log_run(
                    today,
                    OUTCOME_CLOSED,
                    f"no count for {names or 'nothing (count complete, order never released)'}; "
                    f"told {reached} of {everyone} staff",
                )
                if reached == 0:
                    logger.error("The closing text reached no staff number")
        except Exception:
            logger.exception("Could not check or send the stock count follow-up")
            return []

    if release:
        logger.warning("The count is complete but the order never went — placing it now")
        run_weekly_order()
        return []
    return missing


@app.route("/trigger")
def manual_trigger():
    """Secret URL to manually fire the weekly order job. Add &force=1 to send
    even though an order already went out this week."""
    key = request.headers.get("X-Trigger-Key") or request.args.get("key", "")
    if not TRIGGER_KEY or not hmac.compare_digest(key.encode(), TRIGGER_KEY.encode()):
        return "Forbidden", 403
    outcome = run_weekly_order(force=request.args.get("force") == "1")
    return f"Order job finished: {outcome}.", 200


# Set up the scheduler
scheduler = BackgroundScheduler(timezone=MELBOURNE_TZ)
scheduler.add_job(
    run_weekly_order,
    CronTrigger(day_of_week="wed", hour=9, minute=0, timezone=MELBOURNE_TZ),
    id="weekly_order",
    replace_existing=True,
    # A process that is busy or stalled at 9:00 runs the job late instead of dropping it
    # (the default grace is one second). A restart across 9:00 is covered by
    # _missed_todays_run below. Running late is safe: a handled week is skipped.
    misfire_grace_time=3 * 60 * 60,
    coalesce=True,
)


def _schedule_follow_ups() -> None:
    """Book the reminder and the closing text for the request for a count that is open now.

    Timed from the request, not fixed to Wednesday 1pm and Thursday 9am: a request made by a
    late catch-up run or a manual trigger gets the same four hours and the same full day,
    so "within 24 hours" in the first text is true whenever it is sent. For the normal 9:00
    Wednesday run that is 1pm Wednesday and 9am Thursday. Called again at startup, because
    these jobs live in memory and a restart would otherwise drop both texts."""
    try:
        request = _open_request(_melbourne_today())
        if request is None:
            return
        now = datetime.now(timezone.utc)
        soon = now + timedelta(minutes=1)
        if now < request.at + WAIT_FOR_COUNT:
            # Still inside the wait. After a restart past the 4-hour mark this runs a minute
            # from now rather than not at all: the reminder is also what places the order
            # when the count is complete but the thread releasing it died in the restart.
            # It never texts a second reminder for the same request.
            scheduler.add_job(
                chase_missing_count,
                DateTrigger(run_date=max(request.at + REMIND_AFTER, soon)),
                id="chase_count",
                replace_existing=True,
                misfire_grace_time=60 * 60,
            )
        scheduler.add_job(
            chase_missing_count,
            # A deadline already passed (the process was down) closes a minute from now.
            DateTrigger(run_date=max(request.at + WAIT_FOR_COUNT, soon)),
            kwargs={"final": True},
            id="chase_count_final",
            replace_existing=True,
            misfire_grace_time=None,  # however late, the week must still be closed and staff told
        )
    except Exception:
        logger.exception("Could not schedule the follow-ups for the stock count request")


def _missed_todays_run(now: datetime) -> bool:
    """True if it is order day, past 9:00 at the cafe, and the job has not run today —
    i.e. the process was down or restarting when the scheduler should have fired."""
    local = now.astimezone(MELBOURNE_TZ)
    if local.weekday() != ORDER_WEEKDAY or local.hour < 9:
        return False
    last_run = storage.get_last_run(ALL_OUTCOMES)
    return last_run is None or last_run.run_date != local.date()


if __name__ == "__main__":
    storage.init_db()
    scheduler.start()
    logger.info("Scheduler started — weekly order runs every Wednesday at 9:00 AM Melbourne time")
    _schedule_follow_ups()  # a request for a count that was open before the restart
    if _missed_todays_run(datetime.now(timezone.utc)):
        logger.warning("Today's 9:00 order run was missed — running it now")
        threading.Thread(target=run_weekly_order, daemon=True).start()
    logger.info("Flask app starting...")
    app.run(host="0.0.0.0", port=5000, debug=False)
