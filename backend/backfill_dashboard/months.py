from __future__ import annotations

from datetime import datetime, timezone


ORCHESTRATOR_FIRST_MONTH = (2024, 8)


def orchestrator_month_index(year: int, month: int) -> int:
    """Return the stable month index anchored at August 2024."""
    if not 1 <= month <= 12:
        raise ValueError("month must be in [1, 12]")
    first_year, first_month = ORCHESTRATOR_FIRST_MONTH
    index = (year - first_year) * 12 + month - first_month
    if index < 0:
        raise ValueError(
            f"{year}-{month:02d} predates the ULRPM orchestrator origin "
            f"{first_year}-{first_month:02d}."
        )
    return index


def month_for_index(index: int) -> tuple[int, int]:
    if not isinstance(index, int) or index < 0:
        raise ValueError("month_index must be a non-negative integer")
    first_year, first_month = ORCHESTRATOR_FIRST_MONTH
    offset = first_month - 1 + index
    return first_year + offset // 12, offset % 12 + 1


def orchestrator_months(now: datetime | None = None) -> list[tuple[int, int]]:
    """Return supported months through the current UTC calendar month.

    Evaluation happens on every call, so a long-running dashboard rolls into
    a new month without a code change or process restart.
    """
    current = now or datetime.now(timezone.utc)
    last_index = orchestrator_month_index(current.year, current.month)
    return [month_for_index(index) for index in range(last_index + 1)]


def current_month_index(now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    return orchestrator_month_index(current.year, current.month)


def month_window_for_index(
    index: int,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return a UTC month window, capped at the live time for this month."""
    current = now or datetime.now(timezone.utc)
    current_utc = (
        current.astimezone(timezone.utc)
        if current.tzinfo is not None
        else current.replace(tzinfo=timezone.utc)
    )
    if index > current_month_index(current_utc):
        raise ValueError("month_index cannot point to a future month")
    year, month = month_for_index(index)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    next_month = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    return start, min(next_month, current_utc)
