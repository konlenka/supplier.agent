import hmac
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

import storage
from adjustments import get_total_adjustment
from config import (
    EMPLOYEE_PHONE_NUMBERS,
    MIN_DAYS_BETWEEN_ORDERS,
    STOCK_TARGETS,
    STALE_THRESHOLD_DAYS,
    SUPPLIER_PHONE_NUMBER,
    TRIGGER_KEY,
)
from models import JobRun, OrderLine, StockLevel
from order_calculator import calculate_order, cap_to_calculated, format_order_message
from ordering_agent import run_order_agent
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
ALL_OUTCOMES = (
    OUTCOME_ORDERED,
    OUTCOME_NO_ORDER_NEEDED,
    OUTCOME_STALE,
    OUTCOME_SKIPPED,
    OUTCOME_FAILED,
    OUTCOME_SEND_UNCONFIRMED,
)
# Outcomes after which the job must not order again in the same week without force.
HANDLED_OUTCOMES = (OUTCOME_ORDERED, OUTCOME_NO_ORDER_NEEDED, OUTCOME_SEND_UNCONFIRMED)

ORDER_WEEKDAY = 2  # Wednesday (Monday is 0) — matches the scheduler below
FORCE_COOLDOWN = timedelta(minutes=10)

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
        resp.message(
            "Could not parse stock levels. Please try again with format:\n"
            "Almond: X, Oat: X, Soy: X, LF: X bottles, Coconut: X bottles"
        )
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


def _stale_items(current_stock: dict[str, StockLevel], as_of: datetime) -> list[str]:
    """Labels of items with no count, or a count older than the staleness threshold at `as_of`.
    Checked per item: one fresh item must not make last month's count of another look current."""
    cutoff = as_of - timedelta(days=STALE_THRESHOLD_DAYS)
    stale = []
    for item_key, target in STOCK_TARGETS.items():
        level = current_stock.get(item_key)
        if level is None or level.reported_at is None or level.reported_at < cutoff:
            stale.append(target["label"])
    return stale


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


def _waiting_run(today: date) -> JobRun | None:
    """This week's run that stopped to ask staff for a stock count, if nothing has run since."""
    last_run = storage.get_last_run(ALL_OUTCOMES)
    if last_run and last_run.outcome == OUTCOME_STALE and _order_week(last_run.run_date) == _order_week(today):
        return last_run
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
    return _stale_items(storage.get_current_stock(), waiting.at)


def _notify_employees(body: str) -> None:
    """Text every employee. One bad number must not stop the others getting the message."""
    for phone in EMPLOYEE_PHONE_NUMBERS:
        try:
            send_sms(phone, body)
        except Exception:
            logger.exception("Failed to send SMS to employee %s", phone)


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

    if not force and _already_handled(today):
        logger.warning("This week's order was already handled — not sending again")
        return OUTCOME_SKIPPED, "already handled this week"
    if force:
        # force is for a deliberate re-send, not for a double-click or a browser retry.
        last_sent = storage.get_last_run((OUTCOME_ORDERED,))
        if last_sent and datetime.now(timezone.utc) - last_sent.at < FORCE_COOLDOWN:
            logger.warning("Forced run refused — an order went out moments ago")
            return OUTCOME_SKIPPED, "forced run refused: an order was sent minutes ago"

    # Check every item has a fresh count
    current_stock = storage.get_current_stock()
    stale = _stale_items(current_stock, _freshness_as_of(today))
    if stale:
        logger.warning("Stock data is stale or missing for %s — requesting update", stale)
        _notify_employees(
            "Hi! It's ordering day but we don't have a recent stock count for: "
            f"{', '.join(stale)}. Please send current stock levels ASAP — "
            "the order will go out as soon as the count is in.\n"
            "Format: Almond: X, Oat: X, Soy: X, LF: X bottles, Coconut: X bottles"
        )
        return OUTCOME_STALE, f"no fresh count for {', '.join(stale)}"

    # The arithmetic is the ceiling; the agent can only order at or below it.
    calculated = calculate_order(current_stock, today)
    try:
        order_lines, cap_notes = cap_to_calculated(run_order_agent(current_stock, today), calculated)
    except Exception as e:
        logger.error("Ordering agent failed (%s) — falling back to calculate_order", e)
        order_lines, cap_notes = calculated, [f"agent failed, used calculation: {e!r}"]
    if calculated and not order_lines:
        # Stock is short and the agent ordered nothing. Trimming an order is its call;
        # cancelling one is not — staff would be told stock is fine when it isn't.
        order_lines = calculated
        cap_notes.append("agent ordered nothing while stock is short, used calculation")
    for note in cap_notes:
        logger.warning("Order guardrail: %s", note)

    def quantities(lines: list[OrderLine]) -> str:
        return ", ".join(f"{ol.item}={ol.quantity}" for ol in lines) or "nothing"

    detail = "; ".join(
        [f"sent: {quantities(order_lines)}", f"calculated: {quantities(calculated)}", *cap_notes]
    )

    if not order_lines:
        logger.info("All stock levels are sufficient — no order needed this week")
        _notify_employees("Stock levels are good — no order needed this week.")
        return OUTCOME_NO_ORDER_NEEDED, detail

    # Format and send order to supplier
    adjustment = get_total_adjustment(today)
    order_message = format_order_message(order_lines, today, adjustment)

    try:
        send_sms(SUPPLIER_PHONE_NUMBER, order_message)
    except Exception as e:
        raise SupplierSendUnconfirmed(detail) from e
    logger.info("Order sent to supplier: %s", order_message)

    # The order is out. Nothing below may raise, or the caller would tell staff it wasn't sent.
    try:
        storage.save_order(today, order_lines)
    except Exception:
        logger.exception("Order was sent but could not be saved to order history")
    try:
        _notify_employees(build_employee_confirmation(order_lines, current_stock, today))
    except Exception:
        logger.exception("Order was sent but the staff confirmation could not be built")

    return OUTCOME_ORDERED, detail


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
    if _missed_todays_run(datetime.now(timezone.utc)):
        logger.warning("Today's 9:00 order run was missed — running it now")
        threading.Thread(target=run_weekly_order, daemon=True).start()
    logger.info("Flask app starting...")
    app.run(host="0.0.0.0", port=5000, debug=False)
