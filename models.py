from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class StockLevel:
    item: str           # e.g. "almond_milk"
    quantity: float     # numeric amount
    unit: str           # "boxes" or "bottles"
    reported_at: datetime | None = None


@dataclass
class JobRun:
    run_date: date      # the cafe's date when the order job ran
    outcome: str        # one of app.OUTCOME_*
    at: datetime        # when it ran (UTC)


@dataclass
class OrderLine:
    item: str           # e.g. "almond_milk"
    label: str          # e.g. "Almond Milk"
    quantity: int       # number of boxes to order


@dataclass
class Approval:
    id: int
    order_date: date                # the cafe's date when the approver was asked
    order_lines: list[OrderLine]    # the order exactly as the approver was shown it
    counts: dict[str, StockLevel]   # the stock counts it was worked out from
    requested_at: datetime          # when the approver was asked (UTC)
