from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backfill_dashboard.windows import (
    SplitSpec,
    full_month_windows,
    normalize_range,
    split_windows,
    windows_for_selection,
)


def test_normalize_range_date_only_and_offsets():
    start, end = normalize_range("2026-03-01", "2026-03-15")
    assert start == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 15, tzinfo=timezone.utc)

    start, end = normalize_range("2026-03-01T00:00:00Z", "2026-03-15T00:00:00Z")
    assert start == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 15, tzinfo=timezone.utc)

    start, end = normalize_range("2026-03-01T00:00:00+03:00", "2026-03-15T00:00:00+03:00")
    assert start == datetime(2026, 2, 28, 21, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 14, 21, 0, tzinfo=timezone.utc)

    # Naive + aware mix must not raise.
    start, end = normalize_range("2026-03-01T00:00:00", "2026-03-15T00:00:00Z")
    assert start.tzinfo == timezone.utc and end.tzinfo == timezone.utc


def test_normalize_range_rejects_and_caps():
    with pytest.raises(ValueError, match="until must be after since"):
        normalize_range("2026-03-15", "2026-03-15")
    with pytest.raises(ValueError, match="origin"):
        normalize_range("2024-07-01", "2024-08-15")
    now = datetime(2026, 3, 10, 5, 30, tzinfo=timezone.utc)
    start, end = normalize_range("2026-03-01", "2026-04-01", now=now)
    assert end == datetime(2026, 3, 10, 6, 0, tzinfo=timezone.utc)
    start, end = normalize_range("2026-03-01T00:20:00Z", "2026-03-01T00:40:00Z", now=now)
    assert start == datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 1, 1, 0, tzinfo=timezone.utc)


def test_month_boundaries_and_year_rollover():
    start, end = normalize_range("2026-03-25", "2026-04-08")
    windows = split_windows(start, end, SplitSpec("month"))
    assert [(w.month_index, w.since, w.until) for w in windows] == [
        (19, datetime(2026, 3, 25, tzinfo=timezone.utc), datetime(2026, 4, 1, tzinfo=timezone.utc)),
        (20, datetime(2026, 4, 1, tzinfo=timezone.utc), datetime(2026, 4, 8, tzinfo=timezone.utc)),
    ]
    start, end = normalize_range("2025-12-28", "2026-01-03")
    windows = split_windows(start, end, SplitSpec("month"))
    assert [w.month_index for w in windows] == [16, 17]


def test_leap_and_non_leap_day_split():
    start, end = normalize_range("2028-02-28", "2028-03-01", now=datetime(2028, 3, 2, tzinfo=timezone.utc))
    assert len(split_windows(start, end, SplitSpec("day"))) == 2
    start, end = normalize_range("2026-02-28", "2026-03-01")
    assert len(split_windows(start, end, SplitSpec("day"))) == 1


def test_single_day_and_splits():
    start, end = normalize_range("2026-03-05", "2026-03-06")
    assert len(split_windows(start, end, SplitSpec("month"))) == 1
    start, end = normalize_range("2026-03-01", "2026-03-15")
    assert len(split_windows(start, end, SplitSpec("day"))) == 14
    week = split_windows(start, end, SplitSpec("week"))
    assert [(w.since.day, w.until.day) for w in week] == [(1, 8), (8, 15)]
    cross_start, cross_end = normalize_range("2025-09-24", "2025-10-08")
    cross = split_windows(cross_start, cross_end, SplitSpec("week"))
    assert [(w.since.date().isoformat(), w.until.date().isoformat()) for w in cross] == [
        ("2025-09-24", "2025-10-01"),
        ("2025-10-01", "2025-10-08"),
    ]
    days = split_windows(start, end, SplitSpec("days", days=3))
    assert [int((w.until - w.since).total_seconds() // 86400) for w in days] == [3, 3, 3, 3, 2]
    month = split_windows(start, end, SplitSpec("month"))
    assert len(month) == 1 and (month[0].until - month[0].since) == timedelta(days=14)
    with pytest.raises(ValueError):
        SplitSpec("days", days=0).validated()
    with pytest.raises(ValueError):
        SplitSpec("days", days=32).validated()
    with pytest.raises(ValueError):
        SplitSpec("days", days=None).validated()


def test_split_windows_cover_range_without_gaps():
    import random
    rng = random.Random(42)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    for _ in range(40):
        day0 = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=rng.randint(0, 400))
        span = rng.randint(1, 40)
        start, end = normalize_range(day0.date().isoformat(), (day0 + timedelta(days=span)).date().isoformat(), now=now)
        for mode, days in (("month", None), ("day", None), ("week", None), ("days", 5)):
            windows = split_windows(start, end, SplitSpec(mode, days=days))
            assert windows[0].since == start and windows[-1].until == end
            assert all(a.until == b.since for a, b in zip(windows, windows[1:]))


def test_full_month_windows_caps_current_month():
    windows = full_month_windows([19], now=datetime(2026, 3, 10, 5, 30, tzinfo=timezone.utc))
    assert len(windows) == 1
    assert windows[0].since == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert windows[0].until == datetime(2026, 3, 10, 6, 0, tzinfo=timezone.utc)


def test_windows_for_selection_intersection_and_full_month():
    windows = windows_for_selection(
        since="2026-03-01T00:00:00Z",
        until="2026-03-15T00:00:00Z",
        month_indices=[19],
        split=SplitSpec("month"),
    )
    assert len(windows) == 1 and windows[0].until.day == 15
    windows = windows_for_selection(
        since="2026-03-01T00:00:00Z",
        until="2026-03-15T00:00:00Z",
        month_indices=[18],
        split=SplitSpec("month"),
    )
    assert windows == []
    full = windows_for_selection(
        since="", until="", month_indices=[19], split=SplitSpec("month"),
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    assert full[0].since == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert full[0].until == datetime(2026, 4, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        windows_for_selection(since="2026-03-01", until="", month_indices=None, split=SplitSpec("month"))

def test_windows_for_selection_noncontiguous_months_week_split():
    """Selecting Oct 2025 (14) + Mar 2026 (19) with week split must not invent Nov–Feb."""
    from backfill_dashboard.months import orchestrator_month_index

    assert orchestrator_month_index(2025, 10) == 14
    assert orchestrator_month_index(2026, 3) == 19
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    windows = windows_for_selection(
        since="",
        until="",
        month_indices=[14, 19],
        split=SplitSpec("week"),
        now=now,
    )
    assert windows, "expected week chunks for the two selected months"
    indices = [w.month_index for w in windows]
    assert set(indices) == {14, 19}
    assert all(idx in (14, 19) for idx in indices)
    # Chronological across the whole plan; no Nov–Feb indices.
    assert indices == sorted(indices)
    for earlier, later in zip(windows, windows[1:]):
        assert earlier.since < later.since
        assert earlier.until <= later.since

    by_month: dict[int, list] = {}
    for window in windows:
        by_month.setdefault(window.month_index, []).append(window)
    for month_index, month_windows in by_month.items():
        assert month_windows[0].since == datetime(
            2025 if month_index == 14 else 2026,
            10 if month_index == 14 else 3,
            1,
            tzinfo=timezone.utc,
        )
        # Week chunks inside a month abut (contiguous segment).
        assert all(
            left.until == right.since
            for left, right in zip(month_windows, month_windows[1:])
        )
    # Cross-month gap is expected and allowed by the plan generator.
    assert by_month[14][-1].until < by_month[19][0].since
    assert by_month[14][-1].until == datetime(2025, 11, 1, tzinfo=timezone.utc)
    assert by_month[19][0].since == datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_assert_windows_sequential_allows_gaps_across_months():
    from backfill_dashboard.app import _assert_windows_sequential

    machine = "68594353ce7a21678636844d"
    # Oct week parts contiguous, then a gap to March — must pass.
    _assert_windows_sequential(
        {
            machine: [
                {"month_index": 14, "since": "2025/10/01/00", "until": "2025/10/08/00"},
                {"month_index": 14, "since": "2025/10/08/00", "until": "2025/11/01/00"},
                {"month_index": 19, "since": "2026/03/01/00", "until": "2026/03/08/00"},
                {"month_index": 19, "since": "2026/03/08/00", "until": "2026/04/01/00"},
            ]
        }
    )
    with pytest.raises(ValueError, match="non-overlapping"):
        _assert_windows_sequential(
            {
                machine: [
                    {"month_index": 14, "since": "2025/10/01/00", "until": "2025/10/15/00"},
                    {"month_index": 14, "since": "2025/10/10/00", "until": "2025/10/20/00"},
                ]
            }
        )
    with pytest.raises(ValueError, match="month_index=14 must be contiguous"):
        _assert_windows_sequential(
            {
                machine: [
                    {"month_index": 14, "since": "2025/10/01/00", "until": "2025/10/08/00"},
                    {"month_index": 14, "since": "2025/10/15/00", "until": "2025/10/22/00"},
                ]
            }
        )

