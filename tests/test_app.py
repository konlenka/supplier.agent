import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date, datetime, timedelta, timezone

import pytest

import app as app_module
import storage
from models import OrderLine, StockLevel

SUPPLIER = "+61400000001"
STAFF = ["+61400000002", "+61400000003"]
WEDNESDAY = date(2026, 5, 13)  # no season or holiday adjustment

FULL_STOCK = {
    "almond_milk": StockLevel("almond_milk", 12, "boxes"),
    "oat_milk": StockLevel("oat_milk", 8, "boxes"),
    "soy_milk": StockLevel("soy_milk", 7, "boxes"),
    "lactose_free": StockLevel("lactose_free", 7, "bottles"),
    "coconut": StockLevel("coconut", 5, "bottles"),
}
# Almond 4 boxes short, soy 4 short, coconut 3 bottles short (= 1 box)
LOW_STOCK = {
    **FULL_STOCK,
    "almond_milk": StockLevel("almond_milk", 8, "boxes"),
    "soy_milk": StockLevel("soy_milk", 3, "boxes"),
    "coconut": StockLevel("coconut", 2, "bottles"),
}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """The app with a throwaway database, a fixed date and no real SMS. The order job makes
    no model call; the one model call in the app (reading a staff text) is replaced in `sms`."""
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "data" / "stock.db"))
    storage.init_db()

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(app_module, "send_sms", lambda to, body: sent.append((to, body)) or "SID")
    monkeypatch.setattr(app_module, "SUPPLIER_PHONE_NUMBER", SUPPLIER)
    monkeypatch.setattr(app_module, "EMPLOYEE_PHONE_NUMBERS", STAFF)
    monkeypatch.setattr(app_module, "APPROVER_PHONE_NUMBER", "")  # approval off unless a test turns it on
    monkeypatch.setattr(app_module, "_melbourne_today", lambda: WEDNESDAY)
    return sent


def _to_supplier(sent):
    return [body for to, body in sent if to == SUPPLIER]


def test_order_goes_to_supplier_once_and_staff_are_told(bot):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)

    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED

    supplier_msgs = _to_supplier(bot)
    assert len(supplier_msgs) == 1
    assert "13/5/26" in supplier_msgs[0]
    assert "Almond * 4" in supplier_msgs[0]
    assert "Soy * 4" in supplier_msgs[0]
    assert "Coconut * 1" in supplier_msgs[0]
    assert "Oat * 0" in supplier_msgs[0]
    assert {to for to, _ in bot} == {SUPPLIER, *STAFF}


def test_second_run_in_the_same_week_does_not_reorder(bot):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()

    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED
    assert len(_to_supplier(bot)) == 1


def _set_today(monkeypatch, day):
    monkeypatch.setattr(app_module, "_melbourne_today", lambda: day)


def _backdate_runs(delta):
    """Make every logged run look `delta` older."""
    conn = storage._get_connection()
    try:
        for row in conn.execute("SELECT id, created_at FROM job_runs").fetchall():
            older = (datetime.fromisoformat(row["created_at"]) - delta).isoformat()
            conn.execute("UPDATE job_runs SET created_at = ? WHERE id = ?", (older, row["id"]))
        conn.commit()
    finally:
        conn.close()


def test_force_resends_but_not_on_a_double_click(bot):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()

    # Straight after the order went out: a second click, a browser retry.
    assert app_module.run_weekly_order(force=True) == app_module.OUTCOME_SKIPPED
    assert len(_to_supplier(bot)) == 1

    _backdate_runs(timedelta(minutes=30))
    assert app_module.run_weekly_order(force=True) == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 2


def test_order_week_runs_wednesday_to_tuesday():
    assert app_module._order_week(WEDNESDAY) == WEDNESDAY
    assert app_module._order_week(WEDNESDAY + timedelta(days=2)) == WEDNESDAY  # Friday
    assert app_module._order_week(WEDNESDAY + timedelta(days=6)) == WEDNESDAY  # Tuesday
    assert app_module._order_week(WEDNESDAY + timedelta(days=7)) == WEDNESDAY + timedelta(days=7)


def _backdate_stock(delta):
    """Make every count on file look `delta` older."""
    conn = storage._get_connection()
    try:
        for row in conn.execute("SELECT item, updated_at FROM current_stock").fetchall():
            older = (datetime.fromisoformat(row["updated_at"]) - delta).isoformat()
            conn.execute("UPDATE current_stock SET updated_at = ? WHERE item = ?", (older, row["item"]))
        conn.commit()
    finally:
        conn.close()


def _run_details(outcome):
    conn = storage._get_connection()
    try:
        rows = conn.execute("SELECT detail FROM job_runs WHERE outcome = ? ORDER BY id", (outcome,))
        return [row["detail"] for row in rows.fetchall()]
    finally:
        conn.close()


def test_order_released_late_does_not_cancel_next_wednesdays_order(bot, monkeypatch):
    app_module.run_weekly_order()  # Wednesday: no count -> stale
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=1))  # staff answer early Thursday
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED

    # Same week: no second order.
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=6))
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED

    # Next Wednesday is six days after the Thursday order and must still go out,
    # from a count taken since that delivery.
    _backdate_runs(timedelta(days=6))
    _backdate_stock(timedelta(days=6))
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=7))
    storage.save_stock_report(STAFF[0], "recount", LOW_STOCK)
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 2


def test_no_order_while_the_last_delivery_is_still_on_its_way(bot, sms, monkeypatch):
    """An order went out yesterday (a late release, or a forced run). Whatever counts
    arrive, today's run must not order: no count can include that delivery yet."""
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=6))  # the Tuesday
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    _backdate_runs(timedelta(days=1))
    _backdate_stock(timedelta(days=1))
    # A further text after the order — a correction, the other barista, the routine count.
    storage.save_stock_report(STAFF[1], "count again", LOW_STOCK)
    bot.clear()

    _set_today(monkeypatch, WEDNESDAY + timedelta(days=7))
    assert app_module.run_weekly_order() == app_module.OUTCOME_RECENT_ORDER
    assert _to_supplier(bot) == []
    assert {to for to, _ in bot} == set(STAFF)
    assert "An order went to the supplier on Tuesday 19/5" in bot[0][1]
    assert "automatic milk order is skipped" in bot[0][1]

    # The week is settled, not waiting: no reminder, and a count only records stock.
    bot.clear()
    assert app_module.chase_missing_count() == []
    assert app_module.chase_missing_count(final=True) == []
    reply = sms("another count", LOW_STOCK)
    assert "order" not in reply.lower()
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED
    assert bot == []


def test_count_from_before_the_delivery_was_due_is_asked_for_again(bot):
    """The order went three days ago and the count on file is two days old: recent, but
    taken before the delivery was due. The run asks for a recount instead of ordering."""
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    assert app_module.run_weekly_order(force=True) == app_module.OUTCOME_ORDERED
    _backdate_runs(timedelta(days=3))
    conn = storage._get_connection()
    conn.execute("DELETE FROM job_runs WHERE outcome != ?", (app_module.OUTCOME_ORDERED,))
    conn.execute("UPDATE job_runs SET run_date = ?", ((WEDNESDAY - timedelta(days=3)).isoformat(),))
    conn.commit()
    conn.close()
    _backdate_stock(timedelta(days=2, hours=1))  # after the order, before delivery was due
    bot.clear()

    assert app_module.run_weekly_order() == app_module.OUTCOME_STALE
    assert _to_supplier(bot) == []
    assert "An order went to the supplier on Sunday 10/5" in bot[0][1]
    # The delivery time is an estimate: staff are told to wait for the milk, not just the clock.
    assert "Please count again once that delivery has arrived" in bot[0][1]
    assert len(app_module._still_needed(WEDNESDAY)) == 5  # the SMS path applies the same rule

    storage.save_stock_report(STAFF[0], "recount", LOW_STOCK)
    assert app_module._still_needed(WEDNESDAY) == []
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 1


def test_ordinary_week_with_no_new_count_gets_the_ordinary_request(bot, monkeypatch):
    """Last week's order was delivered long ago; nobody has counted since. That is the plain
    "no recent count" case — the recount wording would wrongly blame last week's order."""
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    _backdate_runs(timedelta(days=7))
    _backdate_stock(timedelta(days=7))
    bot.clear()

    _set_today(monkeypatch, WEDNESDAY + timedelta(days=7))
    assert app_module.run_weekly_order() == app_module.OUTCOME_STALE
    assert "It's ordering day but we don't have a recent stock count" in bot[0][1]
    assert "An order" not in bot[0][1]


def test_unconfirmed_send_also_holds_the_next_order_and_is_described_honestly(bot, monkeypatch):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    real_send = app_module.send_sms

    def supplier_times_out(to, body):
        if to == SUPPLIER:
            raise RuntimeError("read timeout")
        return real_send(to, body)

    monkeypatch.setattr(app_module, "send_sms", supplier_times_out)
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=6))
    assert app_module.run_weekly_order() == app_module.OUTCOME_SEND_UNCONFIRMED
    monkeypatch.setattr(app_module, "send_sms", real_send)
    bot.clear()

    _set_today(monkeypatch, WEDNESDAY + timedelta(days=7))
    assert app_module.run_weekly_order() == app_module.OUTCOME_RECENT_ORDER
    assert _to_supplier(bot) == []
    assert "may not have reached the supplier" in bot[0][1]
    assert "went to the supplier" not in bot[0][1]


def test_count_must_be_taken_once_the_delivery_is_due():
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    due = now - timedelta(hours=10)
    stock = {
        key: StockLevel(key, 1, level.unit, reported_at=due - timedelta(seconds=1))
        for key, level in FULL_STOCK.items()
    }
    stock["soy_milk"].reported_at = due  # counted the moment the delivery was due: usable

    stale = app_module._stale_items(stock, now, delivered_by=due)

    assert "Soy Milk" not in stale and len(stale) == 4
    assert app_module._stale_items(stock, now) == []


def test_forced_run_may_reuse_the_counts_the_last_order_was_made_from(bot):
    """force=1 is a deliberate re-send. It is the only run exempt from the delivery rule."""
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    _backdate_runs(timedelta(minutes=30))
    _backdate_stock(timedelta(minutes=60))  # the count predates the order

    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED  # unforced: handled
    assert app_module.run_weekly_order(force=True) == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 2


def test_forced_run_without_a_fresh_count_does_not_reopen_a_handled_week(bot):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()  # ordered: the week is handled
    _backdate_runs(timedelta(minutes=30))
    _backdate_stock(timedelta(days=5))  # counts have gone stale since
    bot.clear()

    assert app_module.run_weekly_order(force=True) == app_module.OUTCOME_SKIPPED
    assert bot == []  # staff are not told "the order has not gone" when it has
    assert app_module._open_request(WEDNESDAY) is None
    assert app_module.chase_missing_count() == []
    assert app_module.chase_missing_count(final=True) == []


def test_counts_fresh_when_the_job_asked_still_release_the_order(bot):
    # Everything except coconut was counted 2.5 days before order day.
    partial = {k: v for k, v in LOW_STOCK.items() if k != "coconut"}
    storage.save_stock_report(STAFF[0], "count", partial)
    conn = storage._get_connection()
    old = (datetime.now(timezone.utc) - timedelta(days=2, hours=12)).isoformat()
    conn.execute("UPDATE current_stock SET updated_at = ?", (old,))
    conn.commit()
    conn.close()

    assert app_module.run_weekly_order() == app_module.OUTCOME_STALE
    assert "stock count for: Coconut." in bot[0][1]

    # Staff answer 20 hours later — the earlier counts are now over 3 days old.
    _backdate_runs(timedelta(hours=20))
    conn = storage._get_connection()
    older = (datetime.now(timezone.utc) - timedelta(days=3, hours=8)).isoformat()
    conn.execute("UPDATE current_stock SET updated_at = ?", (older,))
    conn.commit()
    conn.close()
    storage.save_stock_report(STAFF[0], "coconut 2", {"coconut": LOW_STOCK["coconut"]})

    assert app_module._still_needed(WEDNESDAY) == []
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 1


def test_order_job_makes_no_model_call(bot, monkeypatch):
    """The order is arithmetic. If a model client is ever built during the order job,
    a model is deciding quantities again — and nobody checks the order before it sends."""
    import anthropic

    clients_built = []

    def no_model(*args, **kwargs):
        # Recorded as well as raised: a model call wrapped in try/except (as the old
        # ordering agent's was) would swallow the error and still pass a raise-only check.
        clients_built.append(args)
        raise RuntimeError("the order job must not call a model")

    monkeypatch.setattr(anthropic, "Anthropic", no_model)
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)

    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert "Almond * 4" in _to_supplier(bot)[0]
    assert clients_built == []


def test_missed_run_is_detected_only_on_order_day_after_nine(bot):
    def melbourne(day, hour):
        return datetime(day.year, day.month, day.day, hour, 30, tzinfo=app_module.MELBOURNE_TZ)

    assert not app_module._missed_todays_run(melbourne(WEDNESDAY, 8))
    assert app_module._missed_todays_run(melbourne(WEDNESDAY, 10))
    assert not app_module._missed_todays_run(melbourne(WEDNESDAY + timedelta(days=1), 10))

    app_module.run_weekly_order()  # logs a run dated WEDNESDAY
    assert not app_module._missed_todays_run(melbourne(WEDNESDAY, 10))


def test_order_sent_before_the_run_log_existed_still_blocks_a_resend(bot):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    storage.save_order(WEDNESDAY - timedelta(days=1), [OrderLine("almond_milk", "Almond Milk", 4)])

    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED
    assert _to_supplier(bot) == []


def test_no_stock_count_asks_staff_and_orders_nothing(bot):
    assert app_module.run_weekly_order() == app_module.OUTCOME_STALE
    assert _to_supplier(bot) == []
    assert {to for to, _ in bot} == set(STAFF)
    assert "within 24 hours, please order by hand" in bot[0][1]
    assert "An order" not in bot[0][1]  # the recount wording is only for a count that predates a delivery


def test_one_fresh_item_does_not_hide_the_missing_ones(bot):
    storage.save_stock_report(STAFF[0], "almond 8", {"almond_milk": LOW_STOCK["almond_milk"]})

    assert app_module.run_weekly_order() == app_module.OUTCOME_STALE
    assert _to_supplier(bot) == []
    asked_for = bot[0][1].split("\n")[0]
    assert "Oat Milk" in asked_for
    assert "Almond Milk" not in asked_for


def test_stale_items_flags_old_counts():
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    stock = {
        key: StockLevel(key, 1, level.unit, reported_at=now - timedelta(days=1))
        for key, level in FULL_STOCK.items()
    }
    stock["soy_milk"].reported_at = now - timedelta(days=4)
    assert app_module._stale_items(stock, now) == ["Soy Milk"]


def test_full_stock_orders_nothing_and_is_not_repeated(bot):
    storage.save_stock_report(STAFF[0], "count", FULL_STOCK)

    assert app_module.run_weekly_order() == app_module.OUTCOME_NO_ORDER_NEEDED
    assert _to_supplier(bot) == []
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED
    assert len(bot) == len(STAFF)


def test_supplier_send_error_is_not_reported_as_nothing_sent_and_not_resent(bot, monkeypatch):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    sent = bot

    def send(to, body):
        if to == SUPPLIER:
            # e.g. a timeout after Twilio accepted the message: delivered or not, unknown
            raise RuntimeError("read timeout")
        sent.append((to, body))

    monkeypatch.setattr(app_module, "send_sms", send)

    assert app_module.run_weekly_order() == app_module.OUTCOME_SEND_UNCONFIRMED
    assert len(sent) == len(STAFF)
    assert "may not have reached" in sent[0][1]
    assert "nothing was sent" not in sent[0][1]
    # Must not re-send on its own while it is unknown whether the first one arrived.
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED


def test_failure_before_the_send_tells_staff_and_can_be_retried(bot, monkeypatch):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    real_format = app_module.format_order_message

    def broken(*args):
        raise RuntimeError("bug before the send")

    monkeypatch.setattr(app_module, "format_order_message", broken)
    assert app_module.run_weekly_order() == app_module.OUTCOME_FAILED
    assert _to_supplier(bot) == []
    assert "nothing was sent" in bot[0][1]

    monkeypatch.setattr(app_module, "format_order_message", real_format)
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 1


def test_one_bad_staff_number_does_not_block_the_others(bot, monkeypatch):
    sent = bot

    def send(to, body):
        if to == STAFF[0]:
            raise RuntimeError("bad number")
        sent.append((to, body))

    monkeypatch.setattr(app_module, "send_sms", send)
    app_module._notify_employees("hello")
    assert sent == [(STAFF[1], "hello")]


@pytest.fixture
def sms(bot, monkeypatch):
    """POST a staff text to /sms the way Twilio does. Runs the released order inline
    instead of in a thread, so the test sees it."""
    monkeypatch.setattr(app_module, "validate_twilio_request", lambda url, params, sig: True)
    parsed: dict = {}
    monkeypatch.setattr(app_module, "parse_stock_sms", lambda body: parsed[body])

    class InlineThread:
        def __init__(self, target, daemon=None):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(app_module.threading, "Thread", InlineThread)
    client = app_module.app.test_client()

    def post(body, stock, sender=STAFF[0]):
        parsed[body] = stock
        return client.post("/sms", data={"From": sender, "Body": body}).get_data(as_text=True)

    return post


def test_late_count_by_sms_releases_the_waiting_order(bot, sms):
    app_module.run_weekly_order()  # no stock -> stale, staff asked
    bot.clear()

    reply = sms("almond 8", {"almond_milk": LOW_STOCK["almond_milk"]})
    assert "Still need a count for" in reply and "Oat Milk" in reply
    assert _to_supplier(bot) == []

    reply = sms("full count", LOW_STOCK)
    assert "placing this week's order now" in reply
    assert len(_to_supplier(bot)) == 1

    # A further text the same week must not order again.
    reply = sms("full count again", LOW_STOCK)
    assert "order" not in reply.lower()
    assert len(_to_supplier(bot)) == 1


def test_missing_count_gets_one_reminder_and_the_count_still_releases_the_order(bot, sms):
    app_module.run_weekly_order()  # no stock -> stale, staff asked once
    storage.save_stock_report(STAFF[0], "almond 8", {"almond_milk": LOW_STOCK["almond_milk"]})
    bot.clear()

    missing = app_module.chase_missing_count()
    assert "Oat Milk" in missing and "Almond Milk" not in missing
    assert {to for to, _ in bot} == set(STAFF)
    assert "has not gone out yet" in bot[0][1] and "Oat Milk" in bot[0][1]
    # The deadline in the text is the real one: a day after the request, in the café's time.
    request = app_module._open_request(WEDNESDAY)
    ends = (request.at + timedelta(hours=24)).astimezone(app_module.MELBOURNE_TZ)
    spoken = f"{ends.hour % 12 or 12}:{ends.minute:02d}{'am' if ends.hour < 12 else 'pm'} {ends:%A}"
    assert f"isn't in by {spoken}, please order by hand" in bot[0][1]
    assert _run_details(app_module.OUTCOME_CHASED) == [
        "reminder for Oat Milk, Soy Milk, Lactose Free, Coconut; reached 2 of 2 staff"
    ]

    # Run again (a restart, a scheduler retry): staff are not texted the reminder twice.
    bot.clear()
    assert app_module.chase_missing_count() == []
    assert bot == []

    # A reminder is not a run of the order job: the week still reads as waiting,
    # and the count still releases the order.
    reply = sms("full count", LOW_STOCK)
    assert "placing this week's order now" in reply
    assert len(_to_supplier(bot)) == 1


def test_no_count_by_thursday_closes_the_week_and_a_late_count_only_records_stock(bot, sms, monkeypatch):
    app_module.run_weekly_order()  # Wednesday: stale
    bot.clear()

    _backdate_runs(timedelta(hours=24))  # a day has passed
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=1))
    missing = app_module.chase_missing_count(final=True)
    assert len(missing) == 5
    assert {to for to, _ in bot} == set(STAFF)
    assert "has NOT been placed" in bot[0][1] and "order from the supplier by hand" in bot[0][1]
    assert "closed for this week" in bot[0][1]
    assert _run_details(app_module.OUTCOME_CLOSED) == [
        "no count for Almond Milk, Oat Milk, Soy Milk, Lactose Free, Coconut; told 2 of 2 staff"
    ]

    # Staff were told to order by hand. A count texted afterwards must not also order.
    bot.clear()
    reply = sms("full count", LOW_STOCK)
    assert "Stock updated" in reply and "order" not in reply.lower()
    assert app_module._still_needed(WEDNESDAY + timedelta(days=1)) is None
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED
    assert app_module.chase_missing_count(final=True) == []  # closing twice texts nobody
    assert bot == []


def test_the_wait_for_a_count_ends_after_a_day_even_if_the_closing_text_never_ran(bot, sms):
    app_module.run_weekly_order()  # stale
    _backdate_runs(timedelta(hours=25))  # the process was down at Thursday 9am
    bot.clear()

    reply = sms("full count", LOW_STOCK)

    assert "Stock updated" in reply and "order" not in reply.lower()
    assert _to_supplier(bot) == []
    # A reminder that runs late must not act either: no nag, and no order from the late count.
    bot.clear()
    assert app_module.chase_missing_count() == []
    assert bot == []


def test_a_late_reminder_does_not_nag_once_the_wait_is_over(bot):
    app_module.run_weekly_order()  # stale, nothing counted
    _backdate_runs(timedelta(hours=25))
    bot.clear()

    assert app_module.chase_missing_count() == []
    assert bot == []
    assert _run_details(app_module.OUTCOME_CHASED) == []


def test_a_follow_up_that_reaches_nobody_says_so_in_the_run_log(bot, monkeypatch):
    app_module.run_weekly_order()  # stale

    def every_send_fails(to, body):
        raise RuntimeError("twilio down")

    monkeypatch.setattr(app_module, "send_sms", every_send_fails)
    app_module.chase_missing_count()
    _backdate_runs(timedelta(hours=24))
    app_module.chase_missing_count(final=True)

    assert _run_details(app_module.OUTCOME_CHASED)[0].endswith("reached 0 of 2 staff")
    assert _run_details(app_module.OUTCOME_CLOSED)[0].endswith("told 0 of 2 staff")


def test_no_reminder_when_nothing_is_waiting(bot):
    assert app_module.chase_missing_count() == []  # no run yet this week

    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()  # ordered
    bot.clear()
    assert app_module.chase_missing_count() == []
    assert app_module.chase_missing_count(final=True) == []
    assert bot == []


def _follow_up_jobs():
    """The booked follow-ups by id. The scheduler is not running in tests, so a re-booked
    job is queued next to the old one instead of replacing it; the last one queued wins,
    as it does once the scheduler starts."""
    return {job.id: job for job in app_module.scheduler.get_jobs() if job.id.startswith("chase_count")}


def _clear_follow_up_jobs():
    while _follow_up_jobs():
        for job_id in _follow_up_jobs():
            app_module.scheduler.remove_job(job_id)


def test_a_request_for_a_count_books_its_reminder_and_its_closing_text(bot):
    _clear_follow_up_jobs()
    app_module.run_weekly_order()  # stale: staff asked
    request = app_module._open_request(WEDNESDAY)
    jobs = _follow_up_jobs()

    # Timed from the request, so "within 24 hours" in the first text is true whenever
    # the request was made — 9:00 Wednesday, a late catch-up run, or a manual trigger.
    assert jobs["chase_count"].trigger.run_date == request.at + timedelta(hours=4)
    assert jobs["chase_count_final"].trigger.run_date == request.at + timedelta(hours=24)
    # Only the second one closes the week.
    assert jobs["chase_count"].kwargs == {}
    assert jobs["chase_count_final"].kwargs == {"final": True}
    assert jobs["chase_count_final"].misfire_grace_time is None  # late is fine, never dropped


def test_nothing_is_booked_when_no_count_was_asked_for(bot):
    _clear_follow_up_jobs()
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert _follow_up_jobs() == {}


def test_a_restart_rebooks_the_follow_ups_for_an_open_request(bot):
    app_module.run_weekly_order()  # stale
    _clear_follow_up_jobs()  # the jobs lived in memory; the process restarted
    _backdate_runs(timedelta(hours=30))  # and stayed down past the reminder and the deadline

    app_module._schedule_follow_ups()
    jobs = _follow_up_jobs()

    assert "chase_count" not in jobs  # too late to remind
    due_in = jobs["chase_count_final"].trigger.run_date - datetime.now(timezone.utc)
    assert timedelta(0) < due_in <= timedelta(minutes=1)  # the closing text still goes, now


def test_the_weekly_order_is_booked_for_wednesday_nine_melbourne_time():
    job = {job.id: job for job in app_module.scheduler.get_jobs()}["weekly_order"]
    fields = {field.name: str(field) for field in job.trigger.fields}

    assert (fields["day_of_week"], fields["hour"], fields["minute"]) == ("wed", "9", "0")
    assert str(job.trigger.timezone) == "Australia/Melbourne"
    assert job.func is app_module.run_weekly_order and not job.args and not job.kwargs  # never forced
    assert job.misfire_grace_time == 3 * 60 * 60  # a stalled process runs it late, not never
    assert app_module.ORDER_WEEKDAY == 2  # the weekday the rest of the code calls order day


def test_deadline_is_spoken_in_the_cafes_time():
    from models import JobRun

    def deadline(utc):
        return app_module._deadline_text(JobRun(WEDNESDAY, app_module.OUTCOME_STALE, utc))

    # Asked 9:00am Wednesday in Melbourne (23:00 UTC Tuesday, AEST)
    assert deadline(datetime(2026, 5, 12, 23, 0, tzinfo=timezone.utc)) == "9:00am Thursday"
    assert deadline(datetime(2026, 5, 13, 2, 30, tzinfo=timezone.utc)) == "12:30pm Thursday"
    assert deadline(datetime(2026, 5, 13, 14, 5, tzinfo=timezone.utc)) == "12:05am Friday"
    assert deadline(datetime(2026, 5, 13, 11, 45, tzinfo=timezone.utc)) == "9:45pm Thursday"


def test_a_restart_inside_the_wait_still_gets_a_complete_count_ordered(bot):
    """Asked at 9:00, the count completed at 3pm, and a restart killed the thread placing
    the order. Startup must book the reminder (which places it), not only the close."""
    app_module.run_weekly_order()  # stale
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)  # complete; nothing released it
    _clear_follow_up_jobs()
    _backdate_runs(timedelta(hours=6))
    _backdate_stock(timedelta(minutes=5))
    bot.clear()

    app_module._schedule_follow_ups()
    reminder = _follow_up_jobs()["chase_count"]

    due_in = reminder.trigger.run_date - datetime.now(timezone.utc)
    assert timedelta(0) < due_in <= timedelta(minutes=1)
    reminder.func(*reminder.args, **reminder.kwargs)  # what the scheduler will run
    assert len(_to_supplier(bot)) == 1


def test_a_closing_job_left_over_from_an_earlier_request_does_not_close_a_new_one(bot):
    app_module.run_weekly_order()  # stale, asked seconds ago
    _clear_follow_up_jobs()
    bot.clear()

    assert app_module.chase_missing_count(final=True) == []  # a stale job fires early

    assert bot == []  # the wait that staff were promised is not cut short
    assert app_module._open_request(WEDNESDAY) is not None
    assert _run_details(app_module.OUTCOME_CLOSED) == []
    assert "chase_count_final" in _follow_up_jobs()  # and the right closing job is booked again


def test_count_that_arrives_while_the_run_is_asking_for_it_still_gets_ordered(bot, monkeypatch):
    """The 9:00 run reads the stock (nothing there), and while it is texting staff the
    count lands. That text got "Stock updated" — no run was waiting yet. Without a
    re-check the run logs "stale" over a complete count and no order ever goes."""
    real_notify = app_module._notify_employees

    def count_lands_mid_run(body):
        sent = real_notify(body)
        if "don't have a recent stock count" in body:
            storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
        return sent

    monkeypatch.setattr(app_module, "_notify_employees", count_lands_mid_run)

    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 1
    assert app_module._open_request(WEDNESDAY) is None


def test_reminder_places_the_order_if_the_count_is_complete_but_it_never_went(bot):
    """The release thread died (a restart) after the count completed the set."""
    app_module.run_weekly_order()  # stale
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)  # complete, but nothing released it
    bot.clear()

    assert app_module.chase_missing_count() == []
    assert len(_to_supplier(bot)) == 1
    assert _run_details(app_module.OUTCOME_CHASED) == []  # it ordered; it did not nag


def test_closing_always_ends_the_week_with_a_text_even_if_the_count_is_complete(bot, sms):
    app_module.run_weekly_order()  # stale
    _backdate_runs(timedelta(hours=25))  # the deadline has passed
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)  # arrived after it
    bot.clear()

    app_module.chase_missing_count(final=True)

    assert _to_supplier(bot) == []  # too late to order automatically
    assert {to for to, _ in bot} == set(STAFF)
    assert bot[0][1].startswith("This week's milk order has NOT been placed")
    assert "No stock count came in" not in bot[0][1]  # one did; it was late
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED  # the week is closed


def test_follow_ups_use_the_delivery_rule_too(bot):
    """A request made because the counts predate a delivery: the reminder must list those
    items as still needed, not treat the recent-but-too-early counts as fine."""
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    assert app_module.run_weekly_order(force=True) == app_module.OUTCOME_ORDERED
    _backdate_runs(timedelta(days=3))
    conn = storage._get_connection()
    conn.execute("UPDATE job_runs SET run_date = ?", ((WEDNESDAY - timedelta(days=3)).isoformat(),))
    conn.commit()
    conn.close()
    _backdate_stock(timedelta(days=2, hours=1))
    assert app_module.run_weekly_order() == app_module.OUTCOME_STALE
    bot.clear()

    assert len(app_module.chase_missing_count()) == 5
    assert "Almond Milk" in bot[0][1]
    assert _to_supplier(bot) == []


def test_forced_run_with_no_count_in_an_unhandled_week_still_asks_staff(bot):
    """force only skips the guards against a second order. With nothing ordered this week
    and no count, it must ask like any other run — not refuse silently."""
    assert app_module.run_weekly_order(force=True) == app_module.OUTCOME_STALE
    assert {to for to, _ in bot} == set(STAFF)
    assert app_module._open_request(WEDNESDAY) is not None


def test_sms_outside_a_waiting_week_only_records_stock(bot, sms):
    reply = sms("full count", LOW_STOCK)
    assert "Stock updated" in reply
    assert bot == []


def test_sms_from_an_unknown_number_or_unparseable_text_saves_nothing(bot, sms, monkeypatch):
    sms("full count", LOW_STOCK, sender="+61499999999")
    assert storage.get_current_stock() == {}

    def cannot_parse(body):
        raise ValueError("No stock items found in message")

    monkeypatch.setattr(app_module, "parse_stock_sms", cannot_parse)
    client = app_module.app.test_client()
    reply = client.post("/sms", data={"From": STAFF[0], "Body": "thanks!"}).get_data(as_text=True)
    assert "Could not parse" in reply
    assert storage.get_current_stock() == {}


def test_confirmation_totals_use_the_unit_staff_count_in():
    order = [OrderLine("almond_milk", "Almond Milk", 4), OrderLine("lactose_free", "Lactose Free", 1)]
    stock = {
        "almond_milk": StockLevel("almond_milk", 8, "boxes"),
        "lactose_free": StockLevel("lactose_free", 7, "bottles"),
        "coconut": StockLevel("coconut", 5, "bottles"),
    }
    msg = app_module.build_employee_confirmation(order, stock, WEDNESDAY)
    totals = msg.split("Total Inventory until next Wednesday:")[1]

    assert "Almond * 12" in totals
    assert "Lactose Free * 15 bottles" in totals  # 7 on hand + one box of 8
    assert "Coconut * 5 bottles" in totals


def test_melbourne_today_is_the_cafes_date_not_the_hosts(monkeypatch):
    class TuesdayNightUTC(datetime):
        @classmethod
        def now(cls, tz=None):
            # 23:00 UTC Tuesday = 9:00am Wednesday in Melbourne (AEST)
            return datetime(2026, 5, 12, 23, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(app_module, "datetime", TuesdayNightUTC)
    assert app_module._melbourne_today() == date(2026, 5, 13)


def test_trigger_is_off_without_a_configured_key(bot, monkeypatch):
    client = app_module.app.test_client()

    monkeypatch.setattr(app_module, "TRIGGER_KEY", "")
    assert client.get("/trigger?key=").status_code == 403
    assert client.get("/trigger?key=creme123").status_code == 403

    monkeypatch.setattr(app_module, "TRIGGER_KEY", "a-long-random-key")
    assert client.get("/trigger?key=wrong").status_code == 403
    assert bot == []

    assert client.get("/trigger?key=a-long-random-key").status_code == 200
    assert client.get("/trigger", headers={"X-Trigger-Key": "a-long-random-key"}).status_code == 200


# --- Approval before the order goes to the supplier (the trial period) ---

BOSS = "+61400000009"
STAFF_WAITING = "waiting for approval"


@pytest.fixture
def boss(bot, sms, monkeypatch):
    """Approval switched on, with a boss who is not one of the staff numbers. Returns a
    function that texts the bot as the boss and gives back the bot's reply."""
    monkeypatch.setattr(app_module, "APPROVER_PHONE_NUMBER", BOSS)
    client = app_module.app.test_client()

    def text(body, sender=BOSS):
        return client.post("/sms", data={"From": sender, "Body": body}).get_data(as_text=True)

    return text


def _to_boss(sent, number=BOSS):
    return [body for to, body in sent if to == number]


def _backdate_approvals(delta):
    """Make every approval request look `delta` older."""
    conn = storage._get_connection()
    try:
        for row in conn.execute("SELECT id, requested_at FROM order_approvals").fetchall():
            older = (datetime.fromisoformat(row["requested_at"]) - delta).isoformat()
            conn.execute("UPDATE order_approvals SET requested_at = ? WHERE id = ?", (older, row["id"]))
        conn.commit()
    finally:
        conn.close()


def _approvals():
    conn = storage._get_connection()
    try:
        rows = conn.execute("SELECT status, reply FROM order_approvals ORDER BY id").fetchall()
        return [(row["status"], row["reply"]) for row in rows]
    finally:
        conn.close()


def test_with_approval_on_the_order_goes_to_the_boss_and_not_the_supplier(bot, boss):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)

    assert app_module.run_weekly_order() == app_module.OUTCOME_AWAITING_APPROVAL

    assert _to_supplier(bot) == []
    request = _to_boss(bot)
    assert len(request) == 1
    # The order as the supplier would get it, and the counts it was worked out from.
    for line in ("Almond * 4", "Oat * 0", "Soy * 4", "Lactose Free * 0", "Coconut * 1"):
        assert line in request[0]
    for count in ("Almond 8 boxes", "Soy 3 boxes", "Lactose Free 7 bottles", "Coconut 2 bottles"):
        assert count in request[0]
    assert "YES" in request[0] and "NO" in request[0]
    # Staff hear something too: silence on a Wednesday means "it did not run".
    for number in STAFF:
        assert any(STAFF_WAITING in body for to, body in bot if to == number)


def test_counts_are_shown_to_the_boss_the_way_staff_sent_them():
    assert app_module._count_text(StockLevel("coconut", 1, "bottles")) == "1 bottle"
    assert app_module._count_text(StockLevel("oat_milk", 1, "boxes")) == "1 box"
    assert app_module._count_text(StockLevel("oat_milk", 2.5, "boxes")) == "2.5 boxes"
    assert app_module._count_text(StockLevel("coconut", 0, "bottles")) == "0 bottles"
    assert app_module._count_text(None) == "no count"


def test_approval_request_states_the_real_deadline_in_the_cafes_time(bot, boss):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()

    pending = storage.get_pending_approval()
    deadline = pending.requested_at + timedelta(hours=24)
    assert app_module._clock_text(deadline) in _to_boss(bot)[0]


def test_yes_sends_the_supplier_the_order_the_boss_was_shown(bot, boss):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    # A count that lands after the boss was asked must not change what a yes releases.
    storage.save_stock_report(STAFF[0], "later count", {"almond_milk": StockLevel("almond_milk", 0, "boxes")})
    bot.clear()

    reply = boss("Yes")

    supplier_msgs = _to_supplier(bot)
    assert len(supplier_msgs) == 1
    assert "Almond * 4" in supplier_msgs[0] and "Soy * 4" in supplier_msgs[0]
    assert "Sent to the supplier" in reply
    for number in STAFF:
        assert any("Stock ordered:" in body for to, body in bot if to == number)
    assert storage.get_last_run(app_module.ALL_OUTCOMES).outcome == app_module.OUTCOME_ORDERED
    assert storage.get_order_history(1)[0]["items"]["almond_milk"] == 4
    assert _approvals() == [("approved", "Yes")]
    # What staff will have is worked out from the counts the order was made from: 8 + 4.
    totals = _to_boss(bot, STAFF[0])[-1].split("Total Inventory until next Wednesday:")[1]
    assert "Almond * 12" in totals


def test_an_order_asked_late_in_the_week_and_never_answered_does_not_cancel_next_wednesdays(bot, boss, monkeypatch):
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=6))  # Tuesday: a manual trigger
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    assert app_module.run_weekly_order() == app_module.OUTCOME_AWAITING_APPROVAL

    _set_today(monkeypatch, WEDNESDAY + timedelta(days=7))  # the wait ends on Wednesday
    _backdate_approvals(timedelta(hours=25))
    boss("yes")

    # That closed the week the order was asked in, not the one that starts today.
    assert app_module.run_weekly_order() == app_module.OUTCOME_AWAITING_APPROVAL


@pytest.mark.parametrize("body", [
    "yes", "Yes.", "YES!", "y", "yep", "yeah", "yup", "ok", "Okay", "ok thanks", "yes please",
    "yep send it", "approve", "approved", "confirm", "send it", "Send", "go ahead", "sure", "\U0001F44D",
])
def test_replies_read_as_yes(body):
    assert app_module._read_approval_reply(body) is True


@pytest.mark.parametrize("body", [
    "no", "No.", "n", "nope", "nah", "no thanks", "No, too much oat", "not ok", "not yet",
    "don't send", "don’t send it", "stop", "cancel", "reject", "wrong", "\U0001F44E",
    "no problem, send it",  # reads as a no: a wrong no costs a phone call, a wrong yes costs an order
])
def test_replies_read_as_no(body):
    assert app_module._read_approval_reply(body) is False


@pytest.mark.parametrize("body", [
    "", "?", "what's this", "who is this", "yes but make the oat 4", "yes no", "ok wait",
    "send me the count again", "I'll go check the fridge", "go check with Sam first",
    "sure thing mate, what is it", "good morning", "do not send", "maybe", "8 almond 3 soy",
])
def test_replies_that_are_neither_are_not_guessed(body):
    assert app_module._read_approval_reply(body) is None


def test_an_unclear_reply_sends_nothing_and_the_order_stays_open(bot, boss):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    bot.clear()

    reply = boss("who is this?")

    assert "YES" in reply and "NO" in reply
    assert bot == []
    assert "Sent to the supplier" in boss("yes")
    assert len(_to_supplier(bot)) == 1


def test_a_second_yes_does_not_send_a_second_order(bot, boss):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    boss("yes")
    bot.clear()

    reply = boss("yes")

    assert bot == []
    assert "Nothing was sent" in reply


def test_a_yes_after_the_wait_is_over_sends_nothing_and_closes_the_week(bot, boss):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    _backdate_approvals(timedelta(hours=25))
    bot.clear()

    reply = boss("yes")

    assert _to_supplier(bot) == []
    assert "not sent" in reply and "by hand" in reply
    for number in STAFF:
        assert any("NOT been sent" in body and "by hand" in body for to, body in bot if to == number)
    assert storage.get_last_run(app_module.ALL_OUTCOMES).outcome == app_module.OUTCOME_NOT_APPROVED
    # The week is over: neither another yes nor another run sends anything.
    boss("yes")
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED
    assert _to_supplier(bot) == []


def test_a_yes_from_a_staff_number_is_not_an_approval(bot, boss):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    bot.clear()

    boss("yes", sender=STAFF[0])
    boss("yes", sender="+61499999999")

    assert bot == []
    assert storage.get_pending_approval() is not None


RECOUNT = {**FULL_STOCK, "oat_milk": StockLevel("oat_milk", 6, "boxes")}  # oat 2 boxes short


def _held_order_turned_down(bot, boss, reply="No, too much oat"):
    """A week where the order went to the boss and they said no. Returns the bot's answer."""
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    bot.clear()
    return boss(reply)


def test_a_no_sends_nothing_keeps_the_reason_and_asks_staff_to_count_again(bot, boss):
    reply = _held_order_turned_down(bot, boss)

    assert _to_supplier(bot) == []
    assert "nothing was sent" in reply.lower() and "count again" in reply
    assert _approvals() == [("rejected", "No, too much oat")]
    for number in STAFF:
        asked = [body for to, body in bot if to == number]
        assert len(asked) == 1
        assert "not approved" in asked[0] and "count" in asked[0] and "again" in asked[0]
        assert "within 24 hours" in asked[0] and "by hand" in asked[0]
    # Changing their mind afterwards does not release the order that was turned down.
    boss("yes")
    assert _to_supplier(bot) == []


def test_the_count_the_boss_turned_down_is_not_used_again(bot, boss):
    _held_order_turned_down(bot, boss)
    bot.clear()

    assert set(app_module._still_needed(WEDNESDAY)) == {
        "Almond Milk", "Oat Milk", "Soy Milk", "Lactose Free", "Coconut"
    }
    # A restart or the manual trigger must not put the same order in front of the boss again.
    assert app_module.run_weekly_order() == app_module.OUTCOME_STALE
    assert _to_boss(bot) == [] and _to_supplier(bot) == []
    assert "not approved" in bot[0][1] and "count the milk again" in bot[0][1]  # and staff are told why
    # Nor may a forced run: force re-sends an order, it does not overrule the boss's no.
    _backdate_runs(timedelta(minutes=30))
    assert app_module.run_weekly_order(force=True) == app_module.OUTCOME_STALE
    assert _to_boss(bot) == [] and _to_supplier(bot) == []


def test_the_reminder_after_a_no_chases_the_recount_and_does_not_release_the_old_count(bot, boss):
    _held_order_turned_down(bot, boss)
    _backdate_runs(timedelta(hours=5))
    bot.clear()

    still_missing = app_module.chase_missing_count()

    assert len(still_missing) == 5
    assert _to_boss(bot) == [] and _to_supplier(bot) == []
    assert all(body.startswith("Reminder") and "for approval" in body for _, body in bot)
    assert len(bot) == len(STAFF)


def test_a_recount_after_a_no_goes_back_to_the_boss_and_their_yes_sends_it(bot, sms, boss):
    _held_order_turned_down(bot, boss)
    bot.clear()

    reply = sms("almond 12", {"almond_milk": FULL_STOCK["almond_milk"]})
    assert "Still need a count for" in reply and "Oat Milk" in reply
    assert _to_boss(bot) == []  # a part count releases nothing

    reply = sms("recount", RECOUNT)
    assert "approval" in reply
    assert _to_supplier(bot) == []
    second_request = _to_boss(bot)
    assert len(second_request) == 1
    assert "Oat * 2" in second_request[0] and "Almond * 0" in second_request[0]

    assert "Sent to the supplier" in boss("ok")
    supplier_msgs = _to_supplier(bot)
    assert len(supplier_msgs) == 1 and "Oat * 2" in supplier_msgs[0] and "Almond * 0" in supplier_msgs[0]
    assert [status for status, _ in _approvals()] == ["rejected", "approved"]


def test_a_second_no_asks_for_another_recount(bot, sms, boss):
    _held_order_turned_down(bot, boss)
    sms("recount", RECOUNT)
    bot.clear()

    boss("no")

    assert _to_supplier(bot) == []
    assert all("count" in body and "again" in body for _, body in bot) and len(bot) == len(STAFF)
    assert [status for status, _ in _approvals()] == ["rejected", "rejected"]
    # And the recount that follows is asked of the boss a third time.
    bot.clear()
    sms("third count", RECOUNT)
    assert len(_to_boss(bot)) == 1


def test_no_recount_within_a_day_closes_the_week(bot, sms, boss):
    _held_order_turned_down(bot, boss)
    _backdate_runs(timedelta(hours=25))
    bot.clear()

    app_module.chase_missing_count(final=True)

    assert storage.get_last_run(app_module.ALL_OUTCOMES).outcome == app_module.OUTCOME_CLOSED
    assert all("by hand" in body for _, body in bot) and len(bot) == len(STAFF)
    bot.clear()
    assert "Stock updated" in sms("late recount", RECOUNT)  # only records stock now
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED
    assert bot == []


def test_a_no_books_the_reminder_and_closing_text_for_the_recount(bot, boss):
    _clear_follow_up_jobs()
    _held_order_turned_down(bot, boss)

    request = app_module._open_request(WEDNESDAY)
    jobs = _follow_up_jobs()
    assert jobs["chase_count"].trigger.run_date == request.at + timedelta(hours=4)
    assert jobs["chase_count_final"].trigger.run_date == request.at + timedelta(hours=24)


def test_staff_are_told_the_order_goes_for_approval_not_straight_out(bot, boss):
    app_module.run_weekly_order()  # no count: staff asked
    assert "will go for approval as soon as the count is in" in bot[0][1]
    bot.clear()

    _backdate_runs(timedelta(hours=5))
    app_module.chase_missing_count()
    assert "goes for approval as soon as the count is in" in bot[0][1]


def test_without_approval_staff_are_told_the_order_goes_out(bot):
    app_module.run_weekly_order()
    assert "the order will go out as soon as the count is in" in bot[0][1]
    bot.clear()

    _backdate_runs(timedelta(hours=5))
    app_module.chase_missing_count()
    assert "it goes as soon as the count is in" in bot[0][1]


# --- The boss does not answer ---

def _approval_jobs():
    return {job.id: job for job in app_module.scheduler.get_jobs() if job.id.startswith("chase_approval")}


def _clear_approval_jobs():
    while _approval_jobs():
        for job_id in _approval_jobs():
            app_module.scheduler.remove_job(job_id)


def _order_waiting_for_the_boss(bot):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    assert app_module.run_weekly_order() == app_module.OUTCOME_AWAITING_APPROVAL
    bot.clear()
    return storage.get_pending_approval()


def test_asking_the_boss_books_a_reminder_and_a_closing_text(bot, boss):
    _clear_approval_jobs()
    pending = _order_waiting_for_the_boss(bot)
    jobs = _approval_jobs()

    assert jobs["chase_approval"].trigger.run_date == pending.requested_at + timedelta(hours=4)
    assert jobs["chase_approval_final"].trigger.run_date == pending.requested_at + timedelta(hours=24)
    assert jobs["chase_approval"].kwargs == {}
    assert jobs["chase_approval_final"].kwargs == {"final": True}
    assert jobs["chase_approval_final"].misfire_grace_time is None  # late is fine, never dropped


def test_nothing_is_booked_for_the_boss_when_approval_is_off(bot):
    _clear_approval_jobs()
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    assert _approval_jobs() == {}


def test_a_silent_boss_gets_one_reminder_with_the_order_in_it(bot, boss):
    pending = _order_waiting_for_the_boss(bot)

    app_module.chase_approval()
    app_module.chase_approval()  # a re-run of the job must not text twice

    assert _to_supplier(bot) == []
    reminders = _to_boss(bot)
    assert len(reminders) == 1 and len(bot) == 1
    assert reminders[0].startswith("Reminder")
    assert "Almond * 4" in reminders[0] and "YES" in reminders[0]
    assert app_module._clock_text(pending.requested_at + timedelta(hours=24)) in reminders[0]
    # The order is still theirs to approve.
    assert "Sent to the supplier" in boss("yes")


def test_no_answer_in_a_day_closes_the_week_and_tells_the_boss_and_staff(bot, boss):
    _order_waiting_for_the_boss(bot)
    _backdate_approvals(timedelta(hours=24, minutes=1))

    app_module.chase_approval(final=True)

    assert _to_supplier(bot) == []
    assert any("not sent" in body and "by hand" in body for body in _to_boss(bot))
    for number in STAFF:
        assert any("NOT been sent" in body and "by hand" in body for to, body in bot if to == number)
    assert storage.get_last_run(app_module.ALL_OUTCOMES).outcome == app_module.OUTCOME_NOT_APPROVED
    assert [status for status, _ in _approvals()] == ["expired"]
    bot.clear()
    boss("yes")
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED
    assert _to_supplier(bot) == []


def test_a_boss_who_is_also_staff_is_told_once_when_the_week_closes(bot, sms, monkeypatch):
    monkeypatch.setattr(app_module, "APPROVER_PHONE_NUMBER", STAFF[0])
    _order_waiting_for_the_boss(bot)
    _backdate_approvals(timedelta(hours=25))

    app_module.chase_approval(final=True)

    assert len([body for to, body in bot if to == STAFF[0]]) == 1
    assert len(bot) == len(STAFF)


def test_the_boss_is_not_chased_once_they_have_answered(bot, boss):
    _order_waiting_for_the_boss(bot)
    boss("yes")
    bot.clear()

    app_module.chase_approval()
    _backdate_approvals(timedelta(hours=25))
    app_module.chase_approval(final=True)

    assert bot == []
    assert storage.get_last_run(app_module.ALL_OUTCOMES).outcome == app_module.OUTCOME_ORDERED


def test_a_reminder_that_runs_after_the_deadline_does_not_nag(bot, boss):
    _order_waiting_for_the_boss(bot)
    _backdate_approvals(timedelta(hours=25))

    app_module.chase_approval()

    assert bot == []


def test_a_closing_job_left_from_an_earlier_request_does_not_close_a_new_one(bot, boss):
    _order_waiting_for_the_boss(bot)  # asked moments ago
    _clear_approval_jobs()

    app_module.chase_approval(final=True)  # booked for a request that has since been replaced

    assert bot == []
    assert storage.get_pending_approval() is not None
    assert "chase_approval_final" in _approval_jobs()  # and this request keeps its own closing text


def test_a_restart_rebooks_the_follow_ups_for_an_order_still_with_the_boss(bot, boss):
    _order_waiting_for_the_boss(bot)
    _clear_approval_jobs()  # the jobs lived in memory; the process restarted
    _backdate_approvals(timedelta(hours=30))  # and stayed down past the reminder and the deadline

    app_module._schedule_approval_follow_ups()
    jobs = _approval_jobs()

    assert "chase_approval" not in jobs  # too late to remind
    due_in = jobs["chase_approval_final"].trigger.run_date - datetime.now(timezone.utc)
    assert timedelta(0) < due_in <= timedelta(minutes=1)  # the closing text still goes, now


def test_a_yes_cut_off_before_the_send_was_recorded_is_reported_not_assumed(bot, boss):
    pending = _order_waiting_for_the_boss(bot)
    # The process died after the yes was taken and before the send to the supplier was
    # logged: it is unknown whether the supplier has the order.
    storage.decide_approval(pending.id, storage.APPROVAL_APPROVED, "yes")

    app_module._recover_interrupted_send()
    app_module._recover_interrupted_send()  # every restart runs this; it must report once

    assert _to_supplier(bot) == []  # never re-sent on its own
    assert len(_to_boss(bot)) == 1 and "may not have reached" in _to_boss(bot)[0]
    for number in STAFF:
        assert [body for to, body in bot if to == number and "may not have reached" in body] != []
    assert len(bot) == 1 + len(STAFF)
    assert storage.get_last_run(app_module.ALL_OUTCOMES).outcome == app_module.OUTCOME_SEND_UNCONFIRMED


def test_a_yes_that_was_sent_and_recorded_is_left_alone_after_a_restart(bot, boss):
    _order_waiting_for_the_boss(bot)
    boss("yes")
    bot.clear()

    app_module._recover_interrupted_send()

    assert bot == []
    assert storage.get_last_run(app_module.ALL_OUTCOMES).outcome == app_module.OUTCOME_ORDERED


def test_another_run_while_waiting_for_the_boss_asks_and_orders_nothing(bot, boss):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    bot.clear()

    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED  # a restart, the manual trigger
    assert app_module.run_weekly_order(force=True) == app_module.OUTCOME_SKIPPED
    assert bot == []
    # The original request is still the one a yes releases.
    boss("yes")
    assert len(_to_supplier(bot)) == 1


def test_a_request_that_never_reached_the_boss_cannot_be_approved(bot, boss, monkeypatch):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    sent = bot

    def send(to, body):
        if to == BOSS:
            raise RuntimeError("bad number")
        sent.append((to, body))

    monkeypatch.setattr(app_module, "send_sms", send)

    assert app_module.run_weekly_order() == app_module.OUTCOME_FAILED
    assert any("nothing was sent" in body for _, body in sent)  # staff know to order by hand
    assert not any(STAFF_WAITING in body for _, body in sent)
    sent.clear()
    boss("yes")
    assert _to_supplier(sent) == []


def test_supplier_send_error_after_a_yes_is_unconfirmed_and_not_resent(bot, boss, monkeypatch):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    sent = bot
    sent.clear()
    attempts = []

    def send(to, body):
        if to == SUPPLIER:
            attempts.append(body)
            raise RuntimeError("read timeout")
        sent.append((to, body))

    monkeypatch.setattr(app_module, "send_sms", send)

    reply = boss("yes")

    assert "may not have reached" in reply
    for number in STAFF:
        assert any("may not have reached" in body for to, body in sent if to == number)
    assert storage.get_last_run(app_module.ALL_OUTCOMES).outcome == app_module.OUTCOME_SEND_UNCONFIRMED
    boss("yes")
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED
    assert len(attempts) == 1


def test_no_model_reads_the_bosses_reply(bot, boss, monkeypatch):
    """Whether an order goes is decided by a fixed word list, never by a model."""
    import anthropic

    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    model_calls = []
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: model_calls.append("client") or 1 / 0)
    monkeypatch.setattr(app_module, "parse_stock_sms", lambda body: model_calls.append(body) or 1 / 0)

    boss("hmm, what do you think?")
    boss("yep send it")

    assert model_calls == []
    assert len(_to_supplier(bot)) == 1


def test_a_boss_who_also_counts_stock_can_do_both(bot, sms, monkeypatch):
    monkeypatch.setattr(app_module, "APPROVER_PHONE_NUMBER", STAFF[0])

    # Nothing waiting for approval: their text is a stock count like anyone else's.
    assert "Stock updated" in sms("full count", LOW_STOCK, sender=STAFF[0])
    assert storage.get_current_stock()["almond_milk"].quantity == 8

    assert app_module.run_weekly_order() == app_module.OUTCOME_AWAITING_APPROVAL
    client = app_module.app.test_client()
    reply = client.post("/sms", data={"From": STAFF[0], "Body": "ok"}).get_data(as_text=True)
    assert "Sent to the supplier" in reply
    assert len(_to_supplier(bot)) == 1


def test_a_count_that_completes_the_wait_goes_to_the_boss_and_staff_are_told_so(bot, sms, boss):
    app_module.run_weekly_order()  # no count: staff asked
    bot.clear()

    reply = sms("full count", LOW_STOCK)

    assert "approval" in reply and "placing this week's order now" not in reply
    assert _to_supplier(bot) == []
    assert len(_to_boss(bot)) == 1


def test_next_weeks_request_replaces_one_the_boss_never_answered(bot, boss, monkeypatch):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    app_module.run_weekly_order()
    _backdate_runs(timedelta(days=7))
    _backdate_approvals(timedelta(days=7))
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=7))
    storage.save_stock_report(STAFF[0], "count", {**FULL_STOCK, "oat_milk": StockLevel("oat_milk", 6, "boxes")})
    bot.clear()

    assert app_module.run_weekly_order() == app_module.OUTCOME_AWAITING_APPROVAL
    boss("yes")

    supplier_msgs = _to_supplier(bot)
    assert len(supplier_msgs) == 1
    assert "Oat * 2" in supplier_msgs[0] and "Almond * 0" in supplier_msgs[0]
    assert [status for status, _ in _approvals()] == ["superseded", "approved"]


def test_with_no_approver_set_nothing_is_held(bot):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)

    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 1
    assert _approvals() == []
