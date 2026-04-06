import logging
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
    STOCK_TARGETS,
    STALE_THRESHOLD_DAYS,
    SUPPLIER_PHONE_NUMBER,
)
from order_calculator import calculate_order, format_order_message
from ordering_agent import run_order_agent
from sms import send_sms, validate_twilio_request
from stock_parser import format_confirmation, parse_stock_sms

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")

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

    # Send confirmation
    resp = MessagingResponse()
    resp.message(format_confirmation(parsed))
    return str(resp)


def run_weekly_order():
    """Scheduled job: calculate and send weekly order to supplier."""
    logger.info("Running weekly order job...")

    storage.init_db()

    # Check if stock data is fresh enough
    latest = storage.get_latest_report_time()
    now = datetime.now(timezone.utc)

    if latest is None or (now - latest) > timedelta(days=STALE_THRESHOLD_DAYS):
        logger.warning("Stock data is stale or missing — requesting update from employees")
        for phone in EMPLOYEE_PHONE_NUMBERS:
            send_sms(
                phone,
                "Hi! It's ordering day but we don't have a recent stock count. "
                "Please send current stock levels ASAP.\n"
                "Format: Almond: X, Oat: X, Soy: X, LF: X bottles, Coconut: X bottles",
            )
        return

    # Get current stock and calculate order
    current_stock = storage.get_current_stock()
    today = date.today()
    try:
        order_lines = run_order_agent(current_stock, today)
    except Exception as e:
        logger.error("Ordering agent failed (%s) — falling back to calculate_order", e)
        order_lines = calculate_order(current_stock, today)

    if not order_lines:
        logger.info("All stock levels are sufficient — no order needed this week")
        for phone in EMPLOYEE_PHONE_NUMBERS:
            send_sms(phone, "Stock levels are good — no order needed this week.")
        return

    # Format and send order to supplier
    adjustment = get_total_adjustment(today)
    order_message = format_order_message(order_lines, today, adjustment)

    send_sms(SUPPLIER_PHONE_NUMBER, order_message)
    logger.info("Order sent to supplier: %s", order_message)
    storage.save_order(today, order_lines)

    # Confirm to employees
    date_str = f"{today.day}/{today.month}/{str(today.year)[2:]}"
    order_by_item = {ol.item: ol.quantity for ol in order_lines}

    conf_lines = [date_str, "", "Stock ordered:"]
    for item_key, target in STOCK_TARGETS.items():
        short_label = target["label"].replace(" Milk", "")
        qty = order_by_item.get(item_key, 0)
        conf_lines.append(f"{short_label} * {qty}")

    conf_lines.extend(["", "Total Inventory until next Wednesday:"])
    for item_key, target in STOCK_TARGETS.items():
        short_label = target["label"].replace(" Milk", "")
        ordered = order_by_item.get(item_key, 0)
        current = current_stock.get(item_key)
        if current:
            bottles_per_box = target["bottles_per_box"]
            if current.unit == "boxes":
                current_boxes = int(current.quantity)
            else:
                current_boxes = int(current.quantity) // bottles_per_box
        else:
            current_boxes = 0
        conf_lines.append(f"{short_label} * {current_boxes + ordered}")

    confirmation = "\n".join(conf_lines)
    for phone in EMPLOYEE_PHONE_NUMBERS:
        send_sms(phone, confirmation)


# Set up the scheduler
scheduler = BackgroundScheduler(timezone=MELBOURNE_TZ)
scheduler.add_job(
    run_weekly_order,
    CronTrigger(day_of_week="wed", hour=9, minute=0, timezone=MELBOURNE_TZ),
    id="weekly_order",
    replace_existing=True,
)


if __name__ == "__main__":
    storage.init_db()
    scheduler.start()
    logger.info("Scheduler started — weekly order runs every Wednesday at 9:00 AM AEST")
    logger.info("Flask app starting...")
    app.run(host="0.0.0.0", port=5000, debug=False)
