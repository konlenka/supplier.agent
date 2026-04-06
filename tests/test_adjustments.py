import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date
from adjustments import get_holiday_adjustment, get_season_adjustment, get_total_adjustment


def test_summer_adjustment():
    """December, January, February should return 0.20."""
    assert get_season_adjustment(date(2026, 1, 15)) == 0.20
    assert get_season_adjustment(date(2026, 2, 10)) == 0.20
    assert get_season_adjustment(date(2026, 12, 5)) == 0.20


def test_non_summer_adjustment():
    """March through November should return 0.0."""
    assert get_season_adjustment(date(2026, 3, 1)) == 0.0
    assert get_season_adjustment(date(2026, 6, 15)) == 0.0
    assert get_season_adjustment(date(2026, 11, 30)) == 0.0


def test_holiday_adjustment_near_christmas():
    """A Wednesday before Christmas should trigger holiday adjustment."""
    # Dec 23 2026 is a Wednesday, Christmas is Dec 25 (within 7 days)
    assert get_holiday_adjustment(date(2026, 12, 23)) == 0.20


def test_holiday_adjustment_no_holiday():
    """A regular week with no holidays should return 0.0."""
    # Mid-May 2026 — no Victorian holidays nearby
    assert get_holiday_adjustment(date(2026, 5, 13)) == 0.0


def test_holiday_adjustment_near_australia_day():
    """Week containing Australia Day (Jan 26) should trigger."""
    # Jan 21 2026 is a Wednesday, Australia Day is Jan 26 (within 7 days)
    assert get_holiday_adjustment(date(2026, 1, 21)) == 0.20


def test_stacking_adjustments():
    """Holiday + summer should stack to 0.40."""
    # Jan 21 2026: summer + Australia Day nearby
    total = get_total_adjustment(date(2026, 1, 21))
    assert total == 0.40


def test_no_adjustments():
    """A regular week in autumn should have no adjustments."""
    assert get_total_adjustment(date(2026, 5, 13)) == 0.0
