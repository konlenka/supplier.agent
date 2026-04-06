"""
Dry-run script to test the full ordering pipeline locally.
No SMS sent — just prints what would happen.
"""
from datetime import date

import storage
from adjustments import get_total_adjustment
from models import StockLevel
from order_calculator import calculate_order, format_order_message
from stock_parser import parse_stock_sms, format_confirmation

# --- Step 1: Test Claude AI parsing ---
print("=" * 50)
print("STEP 1: Testing SMS parsing with Claude AI")
print("=" * 50)

test_messages = [
    "we have 3 boxes almond, 2 oat, 5 soy, 4 lactose free bottles, 3 coconut bottles",
    "Almond: 8, Oat: 6, Soy: 4, LF: 3 bottles, Coconut: 2 bottles",
]

for msg in test_messages:
    print(f"\nInput: {msg!r}")
    try:
        parsed = parse_stock_sms(msg)
        print(f"Parsed: {format_confirmation(parsed)}")
    except Exception as e:
        print(f"ERROR: {e}")

# --- Step 2: Test order calculation ---
print("\n" + "=" * 50)
print("STEP 2: Testing order calculation")
print("=" * 50)

# Simulate current stock
sample_stock = {
    "almond_milk": StockLevel("almond_milk", 3, "boxes"),
    "oat_milk": StockLevel("oat_milk", 2, "boxes"),
    "soy_milk": StockLevel("soy_milk", 5, "boxes"),
    "lactose_free": StockLevel("lactose_free", 4, "bottles"),
    "coconut": StockLevel("coconut", 3, "bottles"),
}

today = date.today()
adjustment = get_total_adjustment(today)

print(f"\nDate: {today}")
print(f"Adjustment: +{int(adjustment * 100)}%")
print(f"\nCurrent stock:")
for key, level in sample_stock.items():
    print(f"  {key}: {level.quantity} {level.unit}")

order_lines = calculate_order(sample_stock, today)

if order_lines:
    print(f"\nOrder to place:")
    for ol in order_lines:
        print(f"  {ol.label}: {ol.quantity} box{'es' if ol.quantity != 1 else ''}")

    print(f"\n--- Supplier SMS preview ---")
    print(format_order_message(order_lines, today, adjustment))
else:
    print("\nNo order needed — stock is sufficient!")

# --- Step 3: Test database storage ---
print("\n" + "=" * 50)
print("STEP 3: Testing database storage")
print("=" * 50)

storage.init_db()
storage.save_stock_report("+61000000000", "test message", sample_stock)
retrieved = storage.get_current_stock()
print(f"Saved and retrieved {len(retrieved)} items from database")
for key, level in retrieved.items():
    print(f"  {key}: {level.quantity} {level.unit}")

print("\nDry run complete!")
