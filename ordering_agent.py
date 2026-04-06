import json
import logging
from datetime import date

import anthropic

from adjustments import get_holiday_adjustment, get_season_adjustment, get_total_adjustment
from config import ANTHROPIC_API_KEY, STOCK_TARGETS
from models import OrderLine, StockLevel
import storage

logger = logging.getLogger(__name__)

ORDERING_MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 10

SYSTEM_PROMPT = """You are the ordering assistant for Creme Cafe, a Melbourne café.

Your job is to decide how many boxes of alternative milk to order from the supplier each Wednesday.
The order is sent automatically by SMS — there is no human review before it goes out.

You have five tools available:
- get_current_stock: current stock levels as reported by staff
- get_stock_targets: the maximum stock level to maintain for each item
- get_seasonal_adjustment: any percentage uplift to apply based on season and public holidays
- get_order_history: recent weekly orders, to help you spot trends or anomalies
- submit_order: call this when you have decided on the final quantities

Your process:
1. Call get_current_stock.
2. Call get_stock_targets to understand the deficit for each item.
3. Call get_seasonal_adjustment and factor it into your reasoning.
4. Call get_order_history (default 8 weeks) and review trends — if you are about to order
   significantly more or less than recent weeks, consider whether that is justified.
5. Decide on quantities and call submit_order with all five items.

Ordering rules:
- Always order in whole boxes only (the supplier does not accept part-box orders).
- Round deficits up to the nearest full box.
- Apply the seasonal adjustment as a multiplier on the deficit, not on the target.
  Example: deficit of 10 bottles, 20% adjustment → order for 12 bottles → round up to boxes.
- If current stock already meets or exceeds the target, set quantity_boxes to 0 for that item.
- If order history is empty this is the first run — proceed with the calculation normally.
- Do not over-order speculatively beyond what the seasonal adjustment already covers.

You must call submit_order exactly once. Include all five item keys in the order array,
even items you are setting to 0. Include a clear reasoning field explaining your decision."""


TOOL_DEFINITIONS = [
    {
        "name": "get_current_stock",
        "description": "Returns current stock levels for all five milk items as reported by staff.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_stock_targets",
        "description": "Returns the configured maximum stock targets and unit details for each item.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_seasonal_adjustment",
        "description": "Returns the seasonal/holiday adjustment factor and a plain-English reason.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_order_history",
        "description": "Returns the last N weeks of completed orders, newest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "weeks": {
                    "type": "integer",
                    "description": "Number of past weeks to retrieve (1–12). Default is 8.",
                    "default": 8,
                }
            },
            "required": [],
        },
    },
    {
        "name": "submit_order",
        "description": (
            "Finalise and submit the weekly order. "
            "Include all five item keys even if quantity_boxes is 0."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_key": {"type": "string"},
                            "quantity_boxes": {"type": "integer", "minimum": 0},
                        },
                        "required": ["item_key", "quantity_boxes"],
                    },
                },
                "reasoning": {
                    "type": "string",
                    "description": "Brief explanation of the order decision.",
                },
            },
            "required": ["order", "reasoning"],
        },
    },
]


def _execute_tool(
    tool_name: str,
    tool_input: dict,
    current_stock: dict[str, StockLevel],
    order_date: date,
) -> tuple[str, list[OrderLine] | None]:
    """Execute a tool call. Returns (result_json, order_lines).
    order_lines is only non-None for submit_order — signals the loop to stop."""

    if tool_name == "get_current_stock":
        result = {}
        for key in STOCK_TARGETS:
            sl = current_stock.get(key)
            if sl:
                result[key] = {"quantity": sl.quantity, "unit": sl.unit}
            else:
                result[key] = {"quantity": 0, "unit": STOCK_TARGETS[key]["unit"]}
        return json.dumps(result), None

    if tool_name == "get_stock_targets":
        return json.dumps(STOCK_TARGETS), None

    if tool_name == "get_seasonal_adjustment":
        season = get_season_adjustment(order_date)
        holiday = get_holiday_adjustment(order_date)
        total = get_total_adjustment(order_date)
        if total == 0:
            reason = "No seasonal or holiday adjustment applies."
        else:
            parts = []
            if season > 0:
                parts.append("Melbourne summer (Dec–Feb)")
            if holiday > 0:
                parts.append("a Victorian public holiday within 7 days")
            reason = "Adjustment applies for: " + " and ".join(parts) + "."
        result = {
            "total_adjustment": total,
            "breakdown": {"season": season, "holiday": holiday},
            "reason": reason,
        }
        return json.dumps(result), None

    if tool_name == "get_order_history":
        weeks = tool_input.get("weeks", 8)
        history = storage.get_order_history(weeks)
        return json.dumps(history), None

    if tool_name == "submit_order":
        order_items = tool_input.get("order", [])
        reasoning = tool_input.get("reasoning", "")
        logger.info("Agent order reasoning: %s", reasoning)

        expected_keys = set(STOCK_TARGETS.keys())
        provided_keys = {item["item_key"] for item in order_items}
        missing = expected_keys - provided_keys
        if missing:
            error = {
                "status": "error",
                "message": f"Missing items in order: {sorted(missing)}. Include all 5 items.",
            }
            return json.dumps(error), None

        order_lines = []
        for item in order_items:
            item_key = item["item_key"]
            qty = int(item["quantity_boxes"])
            if qty > 0 and item_key in STOCK_TARGETS:
                label = STOCK_TARGETS[item_key]["label"]
                order_lines.append(OrderLine(item=item_key, label=label, quantity=qty))

        result = {
            "status": "accepted",
            "items_ordered": len(order_lines),
            "message": "Order recorded. It will be sent to the supplier now.",
        }
        return json.dumps(result), order_lines

    return json.dumps({"error": f"Unknown tool: {tool_name}"}), None


def run_order_agent(
    current_stock: dict[str, StockLevel],
    order_date: date,
) -> list[OrderLine]:
    """Run the ordering agent and return the list of OrderLines to fulfil.
    Returns an empty list if the agent determines no order is needed."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    messages: list[dict] = []
    final_order: list[OrderLine] | None = None
    iteration = 0

    while final_order is None and iteration < MAX_ITERATIONS:
        iteration += 1
        logger.debug("Ordering agent — iteration %d", iteration)

        response = client.messages.create(
            model=ORDERING_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            raise RuntimeError(
                f"Ordering agent stopped without calling submit_order after {iteration} turns"
            )

        if response.stop_reason == "tool_use":
            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                result_str, order_lines = _execute_tool(
                    block.name, block.input, current_stock, order_date
                )
                logger.debug("Tool %s → %s", block.name, result_str[:200])

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

                if order_lines is not None:
                    final_order = order_lines

            messages.append({"role": "user", "content": tool_results})

            if final_order is not None:
                break

    if final_order is None:
        raise RuntimeError(
            f"Ordering agent did not submit an order within {MAX_ITERATIONS} iterations"
        )

    return final_order
