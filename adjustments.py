from datetime import date, timedelta

import holidays


def get_holiday_adjustment(order_date: date) -> float:
    """Return 0.20 if a Victorian public holiday falls within 7 days of order_date, else 0.0."""
    vic_holidays = holidays.Australia(subdiv="VIC", years=[order_date.year, order_date.year + 1])
    for day_offset in range(1, 8):
        check_date = order_date + timedelta(days=day_offset)
        if check_date in vic_holidays:
            return 0.20
    return 0.0


def get_season_adjustment(order_date: date) -> float:
    """Return 0.20 during Melbourne summer (Dec-Feb), else 0.0."""
    if order_date.month in (12, 1, 2):
        return 0.20
    return 0.0


def get_total_adjustment(order_date: date) -> float:
    """Combined adjustment multiplier. Adjustments stack."""
    return get_holiday_adjustment(order_date) + get_season_adjustment(order_date)
