"""Pure UTC window planning for orchestrated backfill manifests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from .months import month_for_index, month_window_for_index, orchestrator_month_index

ORCHESTRATOR_ORIGIN = datetime(2024, 8, 1, tzinfo=timezone.utc)
MAX_WINDOWS_PER_MACHINE = 93
MAX_WINDOWS_TOTAL = 5000


@dataclass(frozen=True)
class ManifestWindow:
    month_index: int
    year: int
    month: int
    chunk_index: int
    chunk_count: int
    since: datetime  # UTC aware, inclusive
    until: datetime  # UTC aware, exclusive


@dataclass(frozen=True)
class SplitSpec:
    mode: Literal["month", "day", "week", "days"] = "month"
    days: int | None = None

    @classmethod
    def from_mapping(cls, value: SplitSpec | dict | None) -> SplitSpec:
        if value is None:
            return cls()
        if isinstance(value, SplitSpec):
            return value
        mode = str(value.get("mode") or "month")
        days = value.get("days")
        return cls(mode=mode, days=days)  # type: ignore[arg-type]

    def validated(self) -> SplitSpec:
        if self.mode not in {"month", "day", "week", "days"}:
            raise ValueError("split.mode must be one of month, day, week, days")
        if self.mode == "days":
            if self.days is None:
                raise ValueError("split.days is required when mode is 'days'")
            if not isinstance(self.days, int) or isinstance(self.days, bool) or not 1 <= self.days <= 31:
                raise ValueError("split.days must be an integer between 1 and 31")
        return self


def parse_utc(value: str, *, field_name: str) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp or date") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(value: datetime) -> datetime:
    floored = _floor_hour(value)
    if floored == value:
        return value
    return floored + timedelta(hours=1)


def normalize_range(
    since: str,
    until: str,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
    start = _floor_hour(parse_utc(since, field_name="since"))
    end = _ceil_hour(parse_utc(until, field_name="until"))
    now_ceil = _ceil_hour(current)
    if end > now_ceil:
        end = now_ceil
    if end <= start:
        raise ValueError("until must be after since")
    if start < ORCHESTRATOR_ORIGIN:
        raise ValueError(
            f"since must be on or after the ULRPM orchestrator origin "
            f"{ORCHESTRATOR_ORIGIN.date().isoformat()}"
        )
    if start >= current:
        raise ValueError("since must be before now")
    return start, end


def month_windows(
    start: datetime,
    end: datetime,
) -> list[tuple[int, int, int, datetime, datetime]]:
    """Clip [start, end) into UTC calendar-month windows with orchestrator indices."""
    start_utc = start.astimezone(timezone.utc) if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end_utc = end.astimezone(timezone.utc) if end.tzinfo else end.replace(tzinfo=timezone.utc)
    if end_utc <= start_utc:
        raise ValueError("until must be after since")
    windows: list[tuple[int, int, int, datetime, datetime]] = []
    cursor = datetime(start_utc.year, start_utc.month, 1, tzinfo=timezone.utc)
    while cursor < end_utc:
        year, month = cursor.year, cursor.month
        next_month = (
            datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=timezone.utc)
        )
        window_start = max(start_utc, cursor)
        window_end = min(end_utc, next_month)
        if window_start < window_end:
            index = orchestrator_month_index(year, month)
            windows.append((index, year, month, window_start, window_end))
        cursor = next_month
    if not windows:
        raise ValueError(
            "The ULRPM orchestrator supports ranges overlapping "
            f"{ORCHESTRATOR_ORIGIN.date().isoformat()} through the current month."
        )
    return windows


def _chunk_size(split: SplitSpec) -> int | None:
    split = split.validated()
    if split.mode == "month":
        return None
    if split.mode == "day":
        return 1
    if split.mode == "week":
        return 7
    return int(split.days or 0)


def split_windows(start: datetime, end: datetime, split: SplitSpec) -> list[ManifestWindow]:
    split = split.validated()
    month_parts = month_windows(start, end)
    size = _chunk_size(split)
    result: list[ManifestWindow] = []
    for index, year, month, month_start, month_end in month_parts:
        if size is None:
            chunks = [(month_start, month_end)]
        else:
            chunks = []
            cursor = month_start
            while cursor < month_end:
                chunk_end = min(cursor + timedelta(days=size), month_end)
                chunks.append((cursor, chunk_end))
                cursor = chunk_end
        count = len(chunks)
        for chunk_index, (chunk_start, chunk_end) in enumerate(chunks):
            result.append(
                ManifestWindow(
                    month_index=index,
                    year=year,
                    month=month,
                    chunk_index=chunk_index,
                    chunk_count=count,
                    since=chunk_start,
                    until=chunk_end,
                )
            )
    return result


def full_month_windows(
    month_indices: list[int],
    *,
    now: datetime | None = None,
) -> list[ManifestWindow]:
    current = now or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
    windows: list[ManifestWindow] = []
    for index in sorted(set(month_indices)):
        year, month = month_for_index(index)
        raw_start, raw_end = month_window_for_index(index, current)
        next_month = (
            datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=timezone.utc)
        )
        # When capped by live "now", ceil to the next hour (plan: 05:30 → 06:00).
        capped_end = _ceil_hour(raw_end) if raw_end < next_month else raw_end
        if capped_end <= raw_start:
            continue
        windows.append(
            ManifestWindow(
                month_index=index,
                year=year,
                month=month,
                chunk_index=0,
                chunk_count=1,
                since=raw_start,
                until=capped_end,
            )
        )
    return windows


def windows_for_selection(
    *,
    since: str,
    until: str,
    month_indices: list[int] | None,
    split: SplitSpec,
    now: datetime | None = None,
) -> list[ManifestWindow]:
    split = split.validated()
    since_text = (since or "").strip()
    until_text = (until or "").strip()
    if since_text and until_text:
        start, end = normalize_range(since_text, until_text, now=now)
        windows = split_windows(start, end, split)
        if month_indices is not None:
            allowed = set(month_indices)
            windows = [window for window in windows if window.month_index in allowed]
        _enforce_caps(windows)
        return windows
    if not since_text and not until_text:
        if not month_indices:
            raise ValueError("Select at least one machine and month before starting a backfill")
        base = full_month_windows(month_indices, now=now)
        if split.mode == "month":
            _enforce_caps(base)
            return base
        expanded: list[ManifestWindow] = []
        for window in base:
            expanded.extend(split_windows(window.since, window.until, split))
        _enforce_caps(expanded)
        return expanded
    raise ValueError("since and until must both be provided, or both omitted for full-month selection")


def _enforce_caps(windows: list[ManifestWindow]) -> None:
    if len(windows) > MAX_WINDOWS_PER_MACHINE:
        # Cap is also checked per machine by the caller; keep a total guard here.
        pass
    if len(windows) > MAX_WINDOWS_TOTAL:
        raise ValueError(
            f"Request would create {len(windows)} manifests (maximum {MAX_WINDOWS_TOTAL}). "
            "No files were uploaded."
        )


def enforce_per_machine_cap(windows: list[ManifestWindow], *, machine_id: str) -> None:
    if len(windows) > MAX_WINDOWS_PER_MACHINE:
        raise ValueError(
            f"Machine {machine_id} would create {len(windows)} manifests "
            f"(maximum {MAX_WINDOWS_PER_MACHINE} per machine). No files were uploaded."
        )
