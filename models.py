from dataclasses import dataclass
from datetime import datetime


@dataclass
class StockLevel:
    item: str           # e.g. "almond_milk"
    quantity: float     # numeric amount
    unit: str           # "boxes" or "bottles"
    reported_at: datetime | None = None


@dataclass
class OrderLine:
    item: str           # e.g. "almond_milk"
    label: str          # e.g. "Almond Milk"
    quantity: int       # number of boxes to order
