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
    """The app with a throwaway database, a fixed date and no real SMS or model calls."""
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "data" / "stock.db"))
    storage.init_db()

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(app_module, "send_sms", lambda to, body: sent.append((to, body)) or "SID")
    monkeypatch.setattr(app_module, "SUPPLIER_PHONE_NUMBER", SUPPLIER)
    monkeypatch.setattr(app_module, "EMPLOYEE_PHONE_NUMBERS", STAFF)
    monkeypatch.setattr(app_module, "_melbourne_today", lambda: WEDNESDAY)

    def agent_fails(current_stock, order_date):
        raise RuntimeError("no model in tests")

    monkeypatch.setattr(app_module, "run_order_agent", agent_fails)
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


def test_order_released_late_does_not_cancel_next_wednesdays_order(bot, monkeypatch):
    app_module.run_weekly_order()  # Wednesday: no count -> stale
    friday = WEDNESDAY + timedelta(days=2)
    _set_today(monkeypatch, friday)
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED

    # Same week: no second order.
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=6))
    assert app_module.run_weekly_order() == app_module.OUTCOME_SKIPPED

    # Next Wednesday is five days after the Friday order and must still go out.
    _set_today(monkeypatch, WEDNESDAY + timedelta(days=7))
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 2


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

    # Staff answer a day later — the earlier counts are now over 3 days old.
    _backdate_runs(timedelta(days=1))
    conn = storage._get_connection()
    older = (datetime.now(timezone.utc) - timedelta(days=3, hours=12)).isoformat()
    conn.execute("UPDATE current_stock SET updated_at = ?", (older,))
    conn.commit()
    conn.close()
    storage.save_stock_report(STAFF[0], "coconut 2", {"coconut": LOW_STOCK["coconut"]})

    assert app_module._still_needed(WEDNESDAY) == []
    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert len(_to_supplier(bot)) == 1


def test_agent_cannot_cancel_an_order_when_stock_is_short(bot, monkeypatch):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    monkeypatch.setattr(app_module, "run_order_agent", lambda stock, order_date: [])

    assert app_module.run_weekly_order() == app_module.OUTCOME_ORDERED
    assert "Almond * 4" in _to_supplier(bot)[0]
    assert not any("Stock levels are good" in body for _, body in bot)


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


def test_agent_cannot_order_more_than_the_arithmetic(bot, monkeypatch):
    storage.save_stock_report(STAFF[0], "count", LOW_STOCK)
    monkeypatch.setattr(
        app_module,
        "run_order_agent",
        lambda stock, order_date: [
            OrderLine("almond_milk", "Almond Milk", 400),
            OrderLine("oat_milk", "Oat Milk", 9),  # oat is full — nothing to order
            OrderLine("soy_milk", "Soy Milk", 2),  # ordering less is allowed
        ],
    )

    app_module.run_weekly_order()

    msg = _to_supplier(bot)[0]
    assert "Almond * 4" in msg
    assert "Oat * 0" in msg
    assert "Soy * 2" in msg


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
