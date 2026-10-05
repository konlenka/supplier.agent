import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from datetime import date

import pytest

from ordering_agent import _execute_tool

ALL_ZERO = [
    {"item_key": "almond_milk", "quantity_boxes": 0},
    {"item_key": "oat_milk", "quantity_boxes": 0},
    {"item_key": "soy_milk", "quantity_boxes": 0},
    {"item_key": "lactose_free", "quantity_boxes": 0},
    {"item_key": "coconut", "quantity_boxes": 0},
]


def _submit(order):
    result, lines = _execute_tool(
        "submit_order", {"order": order, "reasoning": "test"}, {}, date(2026, 5, 13)
    )
    return json.loads(result), lines


def test_valid_order_is_accepted_and_zero_lines_dropped():
    order = [dict(item) for item in ALL_ZERO]
    order[0]["quantity_boxes"] = 4

    result, lines = _submit(order)

    assert result["status"] == "accepted"
    assert [(ol.item, ol.quantity) for ol in lines] == [("almond_milk", 4)]


@pytest.mark.parametrize(
    "order",
    [
        ALL_ZERO[:4],  # an item missing
        ALL_ZERO + [{"item_key": "almond_milk", "quantity_boxes": 6}],  # same item twice
        ALL_ZERO[:4] + [{"item_key": "cow_milk", "quantity_boxes": 1}],
        ALL_ZERO[:4] + [{"item_key": "coconut", "quantity_boxes": -1}],
        ALL_ZERO[:4] + [{"item_key": "coconut", "quantity_boxes": 1.5}],
        ALL_ZERO[:4] + [{"item_key": "coconut", "quantity_boxes": "2"}],
        ALL_ZERO[:4] + [{"item_key": "coconut"}],
        "almond 4",
    ],
)
def test_malformed_order_is_sent_back_to_the_agent_not_accepted(order):
    result, lines = _submit(order)

    assert result["status"] == "error"
    assert lines is None
