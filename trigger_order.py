"""Manually trigger the weekly order to test supplier SMS."""
from app import run_weekly_order

print("Triggering weekly order now...")
run_weekly_order()
print("Done!")
