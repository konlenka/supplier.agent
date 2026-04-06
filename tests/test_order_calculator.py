import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date
from models import StockLevel
from order_calculator import calculate_order, format_order_message


def test_full_stock_no_order():
    """If stock meets or exceeds targets, no order should be generated."""
    stock = {
        "almond_milk": StockLevel("almond_milk", 12, "boxes"),
        "oat_milk": StockLevel("oat_milk", 8, "boxes"),
        "soy_milk": StockLevel("soy_milk", 7, "boxes"),
        "lactose_free": StockLevel("lactose_free", 7, "bottles"),
        "coconut": StockLevel("coconut", 5, "bottles"),
    }
    # Use a date with no adjustments
    order = calculate_order(stock, date(2026, 5, 13))
    assert order == []


def test_empty_stock_orders_everything():
    """If stock is empty, should order up to target for all items."""
    order = calculate_order({}, date(2026, 5, 13))
    assert len(order) == 5

    order_dict = {ol.item: ol.quantity for ol in order}
    # Almond: target 12 boxes * 6 = 72 bottles / 6 = 12 boxes
    assert order_dict["almond_milk"] == 12
    # Oat: 8 boxes
    assert order_dict["oat_milk"] == 8
    # Soy: 7 boxes
    assert order_dict["soy_milk"] == 7
    # Lactose free: 7 bottles / 8 per box = ceil(0.875) = 1 box
    assert order_dict["lactose_free"] == 1
    # Coconut: 5 bottles / 6 per box = ceil(0.833) = 1 box
    assert order_dict["coconut"] == 1


def test_partial_stock():
    """Should only order the deficit."""
    stock = {
        "almond_milk": StockLevel("almond_milk", 8, "boxes"),
        "oat_milk": StockLevel("oat_milk", 8, "boxes"),  # full
        "soy_milk": StockLevel("soy_milk", 3, "boxes"),
        "lactose_free": StockLevel("lactose_free", 7, "bottles"),  # full
        "coconut": StockLevel("coconut", 2, "bottles"),
    }
    order = calculate_order(stock, date(2026, 5, 13))
    order_dict = {ol.item: ol.quantity for ol in order}

    # Almond deficit: (12-8)*6 = 24 bottles / 6 = 4 boxes
    assert order_dict["almond_milk"] == 4
    # Oat: full, not in order
    assert "oat_milk" not in order_dict
    # Soy deficit: (7-3)*6 = 24 bottles / 6 = 4 boxes
    assert order_dict["soy_milk"] == 4
    # LF: full
    assert "lactose_free" not in order_dict
    # Coconut: 5-2 = 3 bottles / 6 = ceil(0.5) = 1 box
    assert order_dict["coconut"] == 1


def test_summer_adjustment_increases_order():
    """Summer adjustment should increase order by 20%."""
    stock = {
        "almond_milk": StockLevel("almond_milk", 0, "boxes"),
    }
    # Summer date, no holiday
    order = calculate_order(stock, date(2026, 1, 14))
    order_dict = {ol.item: ol.quantity for ol in order}

    # Almond: 72 bottles * 1.2 = 86.4 / 6 = ceil(14.4) = 15 boxes
    assert order_dict["almond_milk"] == 15


def test_bottles_input_for_box_item():
    """If employee reports almond milk in bottles, calculation should still work."""
    stock = {
        "almond_milk": StockLevel("almond_milk", 60, "bottles"),  # = 10 boxes worth
    }
    order = calculate_order(stock, date(2026, 5, 13))
    order_dict = {ol.item: ol.quantity for ol in order}

    # Deficit: 72 - 60 = 12 bottles / 6 = 2 boxes
    assert order_dict["almond_milk"] == 2


def test_format_order_message():
    """Order message should contain date and item lines in simple format."""
    from models import OrderLine
    lines = [
        OrderLine("almond_milk", "Almond Milk", 4),
        OrderLine("oat_milk", "Oat Milk", 3),
    ]
    msg = format_order_message(lines, date(2026, 4, 1), 0.0)
    assert "1/4/26" in msg
    assert "Almond * 4" in msg
    assert "Oat * 3" in msg
    assert "Soy * 0" in msg


def test_format_order_message_with_adjustment():
    """Adjustment value is used for ordering but not shown in the message."""
    from models import OrderLine
    lines = [OrderLine("almond_milk", "Almond Milk", 15)]
    msg = format_order_message(lines, date(2026, 1, 14), 0.20)
    assert "14/1/26" in msg
    assert "Almond * 15" in msg
