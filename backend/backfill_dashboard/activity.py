from __future__ import annotations

from collections.abc import Mapping
from typing import Any


BACKFILLABLE_STATUSES = {"needs_backfill", "backfilled"}


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def effective_activity_status(month: Any) -> str:
    """Resolve scan activity, using row counts only for legacy snapshots.

    Explicit states are authoritative, including ``unknown``. Snapshots from
    before activity tracking omitted the field; only a positive row count can
    establish that such a month was online.
    """
    activity_status = _get(month, "activity_status")
    if activity_status is not None:
        value = str(activity_status).lower()
        return value if value in {"online", "offline", "not_installed", "unknown"} else "unknown"
    row_count = _get(month, "row_count")
    return "online" if isinstance(row_count, (int, float)) and not isinstance(row_count, bool) and row_count > 0 else "unknown"


def is_month_backfillable(month: Any) -> bool:
    return _get(month, "status") in BACKFILLABLE_STATUSES and effective_activity_status(month) == "online"


def _month_label(month: Any) -> str | None:
    partition = _get(month, "partition")
    label = _get(partition, "label")
    if label:
        return str(label)
    year, number = _get(partition, "year"), _get(partition, "month")
    if year is not None and number is not None:
        try:
            return f"{int(year)}-{int(number):02d}"
        except (TypeError, ValueError):
            return None
    return None


def machine_month_coverage(months: list[Any]) -> dict[str, Any]:
    """Derive all coverage/actionability counts from explicit online months.

    This is shared by live scan rollups and cached snapshot normalization so
    denominators and gap metrics cannot diverge between those paths.
    """
    online = [month for month in months if effective_activity_status(month) == "online"]
    complete = [month for month in online if _get(month, "status") == "backfilled"]
    needs = [month for month in online if _get(month, "status") == "needs_backfill"]
    scan_errors = [month for month in months if _get(month, "status") == "scan_error"]
    no_source = [month for month in months if _get(month, "status") == "no_source_data"]
    missing_partitions = sum(
        _get(month, "row_count") is None for month in needs
    )
    missing_features = sum(
        bool(
            _get(month, "missing_features")
            or _get(month, "zero_count_features")
            or _get(month, "partial_features")
            or _get(month, "missing_schema_columns")
        )
        for month in online
    )
    if needs:
        status = "needs_backfill"
        reason = f"{len(needs)} online monthly partition(s) need backfill."
        action = "Export a backfill manifest for the missing/incomplete online months."
    elif scan_errors:
        status = "scan_error"
        reason = f"{len(scan_errors)} monthly partition(s) could not be scanned."
        action = "Fix scan errors, then rerun the status scan."
    elif not online:
        status = "no_source_data"
        reason = "No online Feature Store months are available for this machine."
        action = "Verify installation and raw sample availability before creating a backfill."
    elif len(complete) == len(online):
        status = "backfilled"
        reason = "All online monthly partitions are backfilled."
        action = "No backfill action required."
    else:
        status = "unknown"
        reason = "Machine has mixed non-actionable status across online months."
        action = "Review monthly details."
    first_missing = next((_month_label(month) for month in needs if _month_label(month)), None)
    last_populated = next(
        (_month_label(month) for month in reversed(complete) if _month_label(month)), None
    )
    return {
        "online": online,
        "complete": complete,
        "needs": needs,
        "scan_errors": scan_errors,
        "no_source": no_source,
        "missing_partitions": missing_partitions,
        "missing_features": missing_features,
        "status": status,
        "reason": reason,
        "recommended_action": action,
        "months_expected": len(online),
        "months_complete": len(complete),
        "first_missing_month": first_missing,
        "last_populated_month": last_populated,
    }


def normalize_snapshot_coverage(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a snapshot with coverage normalized from month activity."""
    normalized = dict(snapshot)
    machines = []
    rollups = []
    for source in snapshot.get("machines", []):
        machine = dict(source)
        months = list(machine.get("months", []))
        coverage = machine_month_coverage(months)
        machine.update({
            key: coverage[key]
            for key in (
                "status", "reason", "recommended_action", "months_expected",
                "months_complete", "first_missing_month", "last_populated_month",
            )
        })
        machine["no_source_months"] = len(coverage["no_source"])
        machines.append(machine)
        rollups.append(coverage)
    normalized["machines"] = machines
    old_summary = dict(snapshot.get("summary", {}))
    summary = {
        **old_summary,
        "machine_count": len(machines),
        "fully_backfilled": sum(item["status"] == "backfilled" for item in rollups),
        "needs_backfill": sum(item["status"] == "needs_backfill" for item in rollups),
        "unknown": sum(item["status"] in {"unknown", "scan_error"} for item in rollups),
        "missing_partitions": sum(item["missing_partitions"] for item in rollups),
        "partitions_with_missing_features": sum(item["missing_features"] for item in rollups),
        "partitions_with_scan_errors": sum(len(item["scan_errors"]) for item in rollups),
        "eligible_partitions": sum(item["months_expected"] for item in rollups),
        "completed_eligible_partitions": sum(item["months_complete"] for item in rollups),
        "no_source_partitions": sum(len(item["no_source"]) for item in rollups),
    }
    normalized["summary"] = summary
    return normalized
