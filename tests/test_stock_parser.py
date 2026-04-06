import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch
from stock_parser import parse_stock_sms, format_confirmation
from models import StockLevel


def _mock_anthropic_response(json_text: str):
    """Create a mock Anthropic response."""
    mock_response = MagicMock()
    mock_content = MagicMock()
    mock_content.text = json_text
    mock_response.content = [mock_content]
    return mock_response


@patch("stock_parser.anthropic.Anthropic")
def test_parse_freeform(mock_anthropic_cls):
    """Should parse free-form text into structured stock levels."""
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_client.messages.create.return_value = _mock_anthropic_response(
        '{"almond_milk": {"quantity": 3, "unit": "boxes"}, "oat_milk": {"quantity": 2, "unit": "boxes"}}'
    )

    result = parse_stock_sms("we have 3 almond and 2 oat")

    assert "almond_milk" in result
    assert result["almond_milk"].quantity == 3
    assert result["almond_milk"].unit == "boxes"
    assert result["oat_milk"].quantity == 2


@patch("stock_parser.anthropic.Anthropic")
def test_parse_with_code_block(mock_anthropic_cls):
    """Should handle response wrapped in markdown code blocks."""
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_client.messages.create.return_value = _mock_anthropic_response(
        '```json\n{"soy_milk": {"quantity": 5, "unit": "boxes"}}\n```'
    )

    result = parse_stock_sms("5 soy")
    assert result["soy_milk"].quantity == 5


@patch("stock_parser.anthropic.Anthropic")
def test_parse_bottles_unit(mock_anthropic_cls):
    """Should correctly parse items reported in bottles."""
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_client.messages.create.return_value = _mock_anthropic_response(
        '{"lactose_free": {"quantity": 4, "unit": "bottles"}, "coconut": {"quantity": 3, "unit": "bottles"}}'
    )

    result = parse_stock_sms("4 lactose free bottles and 3 coconut bottles")
    assert result["lactose_free"].unit == "bottles"
    assert result["coconut"].quantity == 3


def test_format_confirmation():
    """Should format a human-readable confirmation message."""
    parsed = {
        "almond_milk": StockLevel("almond_milk", 3, "boxes"),
        "oat_milk": StockLevel("oat_milk", 2, "boxes"),
    }
    msg = format_confirmation(parsed)
    assert "Almond Milk: 3 boxes" in msg
    assert "Oat Milk: 2 boxes" in msg
    assert msg.startswith("Stock updated:")
