import json
import logging
from datetime import datetime, timezone

import anthropic

from config import ANTHROPIC_API_KEY, STOCK_TARGETS
from models import StockLevel

logger = logging.getLogger(__name__)

VALID_UNITS = ("boxes", "bottles")
# Far above anything the cafe holds — only there to stop a typo or a misparse
# ("1200" for "12") being saved as real stock.
MAX_REPORTED_QUANTITY = 200

SYSTEM_PROMPT = """You are a stock level parser for Creme Cafe. Your job is to extract current stock quantities from an SMS message.

The cafe stocks these alternative milks:
- almond_milk: typically reported in boxes (6 bottles per box)
- oat_milk: typically reported in boxes (6 bottles per box)
- soy_milk: typically reported in boxes (6 bottles per box)
- lactose_free: typically reported in bottles (8 bottles per box)
- coconut: typically reported in bottles (6 bottles per box)

Parse the message and return ONLY valid JSON in this exact format:
{
  "almond_milk": {"quantity": <number>, "unit": "boxes"},
  "oat_milk": {"quantity": <number>, "unit": "boxes"},
  "soy_milk": {"quantity": <number>, "unit": "boxes"},
  "lactose_free": {"quantity": <number>, "unit": "bottles"},
  "coconut": {"quantity": <number>, "unit": "bottles"}
}

Rules:
- Only include items that are mentioned in the message
- If the unit is not specified: assume "boxes" for almond_milk, oat_milk, soy_milk and "bottles" for lactose_free, coconut
- If the employee says "bottles" for almond/oat/soy, use "bottles" as the unit
- If the employee says "boxes" for lactose_free/coconut, use "boxes" as the unit
- Return ONLY the JSON object, no other text"""


def parse_stock_sms(message_body: str) -> dict[str, StockLevel]:
    """Use Claude to parse a free-form or structured SMS into stock levels."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": message_body}],
    )

    raw_text = response.content[0].text.strip()

    # Extract JSON if wrapped in markdown code blocks
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        json_lines = [l for l in lines if not l.startswith("```")]
        raw_text = "\n".join(json_lines)

    return _validate_parsed(json.loads(raw_text))


def _validate_parsed(parsed: object) -> dict[str, StockLevel]:
    """Check the model's JSON before it is saved as stock. Raises ValueError on anything
    that isn't a known item with a sane quantity and unit — a bad parse must never
    become a stock level, because the weekly order is calculated from it."""
    if not isinstance(parsed, dict):
        raise ValueError("Parsed stock is not an object")

    now = datetime.now(timezone.utc)
    result: dict[str, StockLevel] = {}
    for key, data in parsed.items():
        if key not in STOCK_TARGETS:
            raise ValueError(f"Unknown stock item: {key!r}")
        if not isinstance(data, dict):
            raise ValueError(f"Malformed entry for {key}")

        quantity = data.get("quantity")
        unit = data.get("unit")
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            raise ValueError(f"Non-numeric quantity for {key}: {quantity!r}")
        if not 0 <= quantity <= MAX_REPORTED_QUANTITY:
            raise ValueError(f"Quantity out of range for {key}: {quantity!r}")
        if unit not in VALID_UNITS:
            raise ValueError(f"Unknown unit for {key}: {unit!r}")

        result[key] = StockLevel(item=key, quantity=quantity, unit=unit, reported_at=now)

    if not result:
        raise ValueError("No stock items found in message")

    return result


def format_confirmation(parsed: dict[str, StockLevel]) -> str:
    """Format parsed stock levels into a confirmation message."""
    parts = []
    for key, level in parsed.items():
        label = STOCK_TARGETS.get(key, {}).get("label", key)
        parts.append(f"{label}: {level.quantity} {level.unit}")

    return "Stock updated: " + ", ".join(parts)
