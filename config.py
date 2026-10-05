import os
import re

from dotenv import load_dotenv

load_dotenv()


def _phone(value: str) -> str:
    """A phone number as Twilio reports a sender: no spaces, dashes or brackets."""
    return re.sub(r"[\s\-()]", "", value)

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

# Supplier
SUPPLIER_PHONE_NUMBER = os.getenv("SUPPLIER_PHONE_NUMBER", "")

# Employee allowlist
EMPLOYEE_PHONE_NUMBERS = [
    p.strip() for p in os.getenv("EMPLOYEE_PHONE_NUMBERS", "").split(",") if p.strip()
]

# Who approves each order before it goes to the supplier, while the bot is on trial.
# Set it and every order waits for their yes; leave it empty and orders go straight out.
# Matched exactly against the sender of a reply, so it is tidied here: typed as
# "+61 400 000 009", no yes would ever be recognised as the approver's.
APPROVER_PHONE_NUMBER = _phone(os.getenv("APPROVER_PHONE_NUMBER", ""))

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Key for the manual /trigger URL. Unset means the URL is switched off.
TRIGGER_KEY = os.getenv("TRIGGER_KEY", "")

# Stock targets — order up to target_max
# unit: "boxes" means the target is in boxes, "bottles" means target is in bottles
STOCK_TARGETS = {
    "almond_milk": {
        "label": "Almond Milk",
        "target_max": 12,
        "unit": "boxes",
        "bottles_per_box": 6,
    },
    "oat_milk": {
        "label": "Oat Milk",
        "target_max": 8,
        "unit": "boxes",
        "bottles_per_box": 6,
    },
    "soy_milk": {
        "label": "Soy Milk",
        "target_max": 7,
        "unit": "boxes",
        "bottles_per_box": 6,
    },
    "lactose_free": {
        "label": "Lactose Free",
        "target_max": 7,
        "unit": "bottles",
        "bottles_per_box": 8,
    },
    "coconut": {
        "label": "Coconut",
        "target_max": 5,
        "unit": "bottles",
        "bottles_per_box": 6,
    },
}

# Cafe details for order messages
CAFE_NAME = "Creme Cafe"
CAFE_ADDRESS = "70-72 Bay Street, Melbourne"

# Database path
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "stock.db")

# Staleness threshold in days — if stock report is older than this, request an update
STALE_THRESHOLD_DAYS = 3

# How long the supplier takes to deliver after the order text. THELO ASSUMPTION, not confirmed
# with the cafe — ask them and set it. Until that long after an order, no stock count can
# include its delivery, so the bot will not order again from one (it would order the same
# shortfall twice). Must stay under 4: in a normal week the next count comes 4+ days later.
DELIVERY_DAYS = 2

# How long an order waits for a missing stock count before the bot gives up and tells staff
# to order by hand. Without an end, a count texted days later still released an order.
WAIT_FOR_COUNT_HOURS = 24

# How long an order waits for the approver's answer. After that it is not sent, whatever
# they reply: a yes days later would release an order worked out from an old count.
WAIT_FOR_APPROVAL_HOURS = 24

# An order sent within this many days blocks another one, so a restart, a retry or the
# manual trigger can't send the supplier the same order twice in one week.
MIN_DAYS_BETWEEN_ORDERS = 6
