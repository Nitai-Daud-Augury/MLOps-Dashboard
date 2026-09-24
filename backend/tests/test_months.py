from datetime import datetime, timezone

import pytest

from backfill_dashboard.months import (
    month_for_index,
    month_window_for_index,
    orchestrator_month_index,
    orchestrator_months,
)


NOW = datetime(2026, 9, 15, 13, 42, tzinfo=timezone.utc)


def test_dynamic_months_extend_through_the_live_utc_month_with_stable_indices():
    months = orchestrator_months(NOW)

    assert months[0] == (2024, 8)
    assert months[-1] == (2026, 9)
    assert len(months) == 26
    assert orchestrator_month_index(2026, 9) == 25
    assert month_for_index(25) == (2026, 9)


def test_current_month_window_is_capped_at_the_live_time():
    assert month_window_for_index(25, NOW) == (
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        NOW,
    )
    assert month_window_for_index(24, NOW) == (
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def test_future_month_window_is_rejected():
    with pytest.raises(ValueError, match="future month"):
        month_window_for_index(26, NOW)
