import math
from datetime import date

from adjustments import get_total_adjustment
from config import STOCK_TARGETS
from models import OrderLine, StockLevel


def _to_bottles(quantity: float, unit: str, bottles_per_box: int) -> float:
    """Convert a quantity to bottles."""
    if unit == "boxes":
        return quantity * bottles_per_box
    return quantity  # already bottles


def calculate_order(
    current_stock: dict[str, StockLevel],
    order_date: date | None = None,
) -> list[OrderLine]:
    """Calculate how many boxes of each item to order.

    Compares current stock against targets, applies seasonal/holiday adjustments,
    and returns a list of OrderLines for items that need restocking.
    """
    if order_date is None:
        order_date = date.today()

    adjustment = get_total_adjustment(order_date)
    order_lines: list[OrderLine] = []

    for item_key, target in STOCK_TARGETS.items():
        bottles_per_box = target["bottles_per_box"]
        target_max = target["target_max"]
        label = target["label"]

        # Convert target to bottles
        if target["unit"] == "boxes":
            target_bottles = target_max * bottles_per_box
        else:
            target_bottles = target_max

        # Convert current stock to bottles
        current = current_stock.get(item_key)
        if current:
            current_bottles = _to_bottles(current.quantity, current.unit, bottles_per_box)
        else:
            current_bottles = 0  # no data, assume empty

        # Calculate deficit
        deficit = target_bottles - current_bottles
        if deficit <= 0:
            continue

        # Apply adjustment
        adjusted_deficit = deficit * (1 + adjustment)

        # Convert to boxes (always order in full boxes, round up)
        boxes_to_order = math.ceil(adjusted_deficit / bottles_per_box)

        order_lines.append(OrderLine(item=item_key, label=label, quantity=boxes_to_order))

    return order_lines


def cap_to_calculated(
    agent_lines: list[OrderLine],
    calculated_lines: list[OrderLine],
) -> tuple[list[OrderLine], list[str]]:
    """Limit the ordering agent's quantities to what the stock arithmetic allows.

    The agent may order less than the calculated deficit (it sees order history) but
    never more — the order goes to the supplier with nobody checking it first.
    Returns the capped lines and a note for each line that was reduced.
    """
    ceiling = {ol.item: ol.quantity for ol in calculated_lines}
    capped: list[OrderLine] = []
    notes: list[str] = []

    for ol in agent_lines:
        limit = ceiling.get(ol.item, 0)
        if ol.quantity > limit:
            notes.append(f"{ol.label}: agent asked for {ol.quantity}, capped to {limit}")
        quantity = min(ol.quantity, limit)
        if quantity > 0:
            capped.append(OrderLine(item=ol.item, label=ol.label, quantity=quantity))

    return capped, notes


def format_order_message(
    order_lines: list[OrderLine],
    order_date: date,
    adjustment: float,
) -> str:
    """Format order lines into an SMS-friendly message for the supplier."""
    from config import STOCK_TARGETS

    date_str = f"{order_date.day}/{order_date.month}/{str(order_date.year)[2:]}"
    order_by_item = {ol.item: ol.quantity for ol in order_lines}

    lines = ["Hey Brent,", "", date_str, ""]
    for item_key, target in STOCK_TARGETS.items():
        short_label = target["label"].replace(" Milk", "")
        qty = order_by_item.get(item_key, 0)
        lines.append(f"{short_label} * {qty}")

    return "\n".join(lines)
