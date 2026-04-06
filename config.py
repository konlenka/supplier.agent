import os
from dotenv import load_dotenv

load_dotenv()

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

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

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
