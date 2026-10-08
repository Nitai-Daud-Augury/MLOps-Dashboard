"""Opt-in, read-only cross-check of FST blob partitions against Databricks.

Enabled with ``FEATURES_CROSSCHECK=feature_store`` (default: off). Azure Blob
(the FST Parquet partitions) stays the single source of truth for every scan
status; this module only *annotates* months with a ``crosscheck`` field and
never changes ``status`` / ``activity_status`` / ``reason``.

Per machine/month it aggregates in SQL (filters pushed down on ``machine_id``
and the ``recorded_at`` range) the row count plus non-null counts of the v2
ultrasonic target features (and their v1 counterparts), then flags:

* ``row_ratio``                - blob rows / feature_store rows outside
                                 ``[1 - tol, 1 + tol]`` (``FEATURES_CROSSCHECK_ROW_TOLERANCE``,
                                 default 0.10)
* ``v2_presence``              - a v2 target feature has data on one side only
* ``missing_in_feature_store`` - blob has rows, feature_store has none
* ``missing_in_blob``          - feature_store has rows, blob has no partition

Staleness gate: one extra query on the bronze mirror
(``DATABRICKS_FEATURE_STORE_BRONZE_TABLE``, default
``dih_prod.bronze_augury_mh_blob.feature_store``) returns the latest
``_file_modification_time`` per ``_source_file``. The bronze job only re-merges
the current and previous month nightly, so a blob file rewritten later (e.g. a
backfill of an older month) is never re-ingested. When the blob file is newer
than bronze's copy, or bronze has no copy of it, the month becomes
``feature_store_stale`` (with ``blob_modified`` / ``bronze_modified``) and any
row/v2 differences are kept only as ``suppressed_flags``. Real mismatches are
reported only when Databricks holds the current file version. If the staleness
query fails, the plain comparison is kept and a warning says staleness could
not be checked.

Not judged: schema completeness. Against the canonical FST contract the
Databricks tables have 23 truly absent columns plus 11 case-duplicate column
pairs that Databricks collapses (it is case-insensitive), so full-schema
completeness remains blob-only and is never compared.

Fail-open: any error yields ``crosscheck.status = "error"`` plus a scan warning;
the scan result is unaffected.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from . import data_source_log as ds_log

DEFAULT_FEATURE_STORE_TABLE = "dih_prod.silver_mh.feature_store"
DEFAULT_BRONZE_TABLE = "dih_prod.bronze_augury_mh_blob.feature_store"
# Bronze _file_modification_time has second precision.
_STALENESS_SLACK = timedelta(seconds=1)
_SOURCE_FILE_RE = re.compile(
    r"machine_id=(?P<machine>[^/]+)/quarter=(?P<year>\d{4})Q(?P<quarter>[1-4])/month=(?P<month>\d{1,2})"
    r"/partition_version=(?P<version>[^/]+)/(?P<file>[^/]+\.parquet)$"
)
DEFAULT_ROW_TOLERANCE = 0.10
SUPPORTED_MODES = ("feature_store",)
NOT_JUDGED_NOTE = (
    "Schema completeness is not compared: against the canonical FST contract feature_store has "
    "23 truly absent columns plus 11 case-duplicate column pairs collapsed by Databricks, so "
    "full-schema completeness stays blob-only. Only row counts and v2 ultrasonic presence are "
    "cross-checked; scan statuses always come from blob."
)
STALENESS_NOTE = (
    "Bronze only re-merges the current and previous month nightly, so blob files rewritten later "
    "(e.g. backfilled older months) are not re-ingested; such months show as feature_store_stale "
    "('Databricks copy is older than blob') instead of a mismatch."
)
_OFF_VALUES = {"", "0", "off", "none", "false", "no", "disabled", "blob"}

Executor = Callable[[str], "tuple[list[str], list[tuple[Any, ...]]]"]


def crosscheck_mode() -> str:
    """Normalized ``FEATURES_CROSSCHECK`` value ('' when off)."""
    raw = (os.getenv("FEATURES_CROSSCHECK") or "").strip().lower()
    return "" if raw in _OFF_VALUES else raw


def crosscheck_enabled() -> bool:
    return crosscheck_mode() in SUPPORTED_MODES


def feature_store_table() -> str:
    from .lifecycle_databricks import _validate_table

    raw = (os.getenv("DATABRICKS_FEATURE_STORE_TABLE") or "").strip()
    return _validate_table(raw or DEFAULT_FEATURE_STORE_TABLE)


def bronze_table() -> str:
    from .lifecycle_databricks import _validate_table

    raw = (os.getenv("DATABRICKS_FEATURE_STORE_BRONZE_TABLE") or "").strip()
    return _validate_table(raw or DEFAULT_BRONZE_TABLE)


def row_tolerance() -> float:
    raw = (os.getenv("FEATURES_CROSSCHECK_ROW_TOLERANCE") or "").strip()
    if not raw:
        return DEFAULT_ROW_TOLERANCE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ROW_TOLERANCE
    return value if value >= 0 else DEFAULT_ROW_TOLERANCE


def crosscheck_timeout_seconds() -> float:
    """Overall ceiling for the cross-check query (default 300s); the SQL path
    also applies lifecycle_databricks' cold/steady timeouts."""
    raw = (os.getenv("FEATURES_CROSSCHECK_TIMEOUT_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else 300.0
    except ValueError:
        value = 300.0
    return value if value > 0 else 300.0


def describe_crosscheck_config() -> dict[str, Any]:
    """Non-secret config for data_sources / startup log."""
    mode = crosscheck_mode()
    info: dict[str, Any] = {
        "enabled": mode in SUPPORTED_MODES,
        "mode": mode or "off",
        "row_tolerance": row_tolerance(),
    }
    try:
        info["table"] = feature_store_table()
    except Exception as exc:  # noqa: BLE001 - invalid table name is reported, not raised
        info["table"] = None
        info["config_error"] = str(exc)[:200]
    try:
        info["bronze_table"] = bronze_table()
    except Exception as exc:  # noqa: BLE001
        info["bronze_table"] = None
        info["config_error"] = str(exc)[:200]
    if mode and mode not in SUPPORTED_MODES:
        info["config_error"] = f"unsupported FEATURES_CROSSCHECK={mode!r}; supported: {', '.join(SUPPORTED_MODES)}"
    return info


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _v1_name(feature: str) -> str | None:
    return feature[: -len("_v2")] if feature.endswith("_v2") else None


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def build_crosscheck_query(
    table: str,
    machine_ids: Iterable[str],
    start: tuple[int, int],
    end: tuple[int, int],
    target_features: Iterable[str],
) -> str:
    """Aggregate per machine/month; machine_id IN + recorded_at range are pushed down."""
    ids = sorted({str(machine_id) for machine_id in machine_ids})
    if not ids:
        raise ValueError("no machine ids")
    v2 = [feature for feature in target_features if feature.endswith("_v2")]
    v1 = [name for name in (_v1_name(feature) for feature in v2) if name]
    aggregates = ["COUNT(*) AS row_count"]
    aggregates += [f"COUNT(`{feature}`) AS `v2__{feature}`" for feature in v2]
    aggregates += [f"COUNT(`{feature}`) AS `v1__{feature}`" for feature in v1]
    end_exclusive = _next_month(*end)
    return (
        "SELECT machine_id, year(recorded_at) AS year, month(recorded_at) AS month, "
        + ", ".join(aggregates)
        + f" FROM {table}"
        + f" WHERE machine_id IN ({', '.join(_quote(value) for value in ids)})"
        + f" AND recorded_at >= TIMESTAMP '{start[0]:04d}-{start[1]:02d}-01 00:00:00'"
        + f" AND recorded_at < TIMESTAMP '{end_exclusive[0]:04d}-{end_exclusive[1]:02d}-01 00:00:00'"
        + " GROUP BY machine_id, year(recorded_at), month(recorded_at)"
    )


def parse_crosscheck_rows(columns: list[str], rows: list[tuple[Any, ...]]) -> dict[tuple[str, int, int], dict[str, Any]]:
    result: dict[tuple[str, int, int], dict[str, Any]] = {}
    for row in rows:
        record = dict(zip(columns, row))
        key = (str(record["machine_id"]).lower(), int(record["year"]), int(record["month"]))
        result[key] = {
            "rows": int(record.get("row_count") or 0),
            "v2": {name[4:]: int(value or 0) for name, value in record.items() if name.startswith("v2__")},
            "v1": {name[4:]: int(value or 0) for name, value in record.items() if name.startswith("v1__")},
        }
    return result


def build_bronze_versions_query(table: str, machine_ids: Iterable[str]) -> str:
    """Latest bronze file version per _source_file for our machines (machine_id pushed down)."""
    ids = sorted({str(machine_id) for machine_id in machine_ids})
    if not ids:
        raise ValueError("no machine ids")
    return (
        "SELECT _source_file, max(_file_modification_time) AS bronze_modified"
        f" FROM {table}"
        f" WHERE machine_id IN ({', '.join(_quote(value) for value in ids)})"
        " GROUP BY _source_file"
    )


def parse_source_file(path: Any) -> tuple[str, int, int] | None:
    """Map a bronze _source_file to (machine_id, year, month) for the scanned file.

    Only ``partition_version=last/part-0.parquet`` (the file the scan reads)
    maps; anything else returns None.
    """
    match = _SOURCE_FILE_RE.search(str(path or "").replace("\\", "/"))
    if not match or match.group("version") != "last" or match.group("file") != "part-0.parquet":
        return None
    year, month = int(match.group("year")), int(match.group("month"))
    if not 1 <= month <= 12 or (month - 1) // 3 + 1 != int(match.group("quarter")):
        return None
    return match.group("machine").lower(), year, month


def _as_utc(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def parse_bronze_versions(columns: list[str], rows: list[tuple[Any, ...]]) -> dict[tuple[str, int, int], datetime]:
    versions: dict[tuple[str, int, int], datetime] = {}
    for row in rows:
        record = dict(zip(columns, row))
        key = parse_source_file(record.get("_source_file"))
        modified = _as_utc(record.get("bronze_modified"))
        if key is None or modified is None:
            continue
        if key not in versions or modified > versions[key]:
            versions[key] = modified
    return versions


def _default_executor(lifecycle_inventory: Any = None) -> Executor:
    """Reuse the lifecycle Databricks provider's SQL path (auth, timeouts, cold retry)."""
    execute = getattr(lifecycle_inventory, "_execute", None)
    if not (callable(execute) and getattr(lifecycle_inventory, "warehouse_id", None)):
        from .lifecycle_databricks import build_databricks_lifecycle_provider

        provider, status = build_databricks_lifecycle_provider()
        if provider is None:
            raise RuntimeError(f"Databricks SQL unavailable for feature_store cross-check (status: {status})")
        execute = provider._execute

    def run(query: str, label: str = "crosscheck") -> "tuple[list[str], list[tuple[Any, ...]]]":
        return execute(query, label=label)

    run.accepts_label = True  # type: ignore[attr-defined]
    return run


def _run(executor: Executor, query: str, label: str):
    if getattr(executor, "accepts_label", False):
        return executor(query, label=label)  # type: ignore[call-arg]
    return executor(query)


@dataclass
class FeatureStoreCrosscheck:
    table: str = field(default_factory=feature_store_table)
    tolerance: float = field(default_factory=row_tolerance)
    executor: Executor | None = None
    lifecycle_inventory: Any = None
    bronze_table: str | None = field(default_factory=bronze_table)

    def fetch(
        self,
        machine_ids: list[str],
        partitions: list[Any],
        target_features: list[str],
    ) -> dict[str, Any]:
        started = time.monotonic()
        if not machine_ids or not partitions:
            return {"data": {}, "elapsed_s": 0.0, "query_ran": False}
        months = sorted({(partition.year, partition.month) for partition in partitions})
        query = build_crosscheck_query(self.table, machine_ids, months[0], months[-1], target_features)
        executor = self.executor or _default_executor(self.lifecycle_inventory)
        staleness: dict[str, Any] = {"status": "skipped", "table": self.bronze_table}

        def bronze() -> dict[tuple[str, int, int], datetime]:
            bronze_started = time.monotonic()
            columns, rows = _run(
                executor, build_bronze_versions_query(str(self.bronze_table), machine_ids), "crosscheck bronze staleness"
            )
            staleness["elapsed_s"] = round(time.monotonic() - bronze_started, 2)
            staleness["files"] = len(rows)
            versions = parse_bronze_versions(columns, rows)
            ds_log.info(
                f"crosscheck staleness: table={self.bronze_table} machines={len(machine_ids)} "
                f"files={len(rows)} mapped={len(versions)} in {staleness['elapsed_s']:.1f}s"
            )
            return versions

        # Both aggregates run concurrently on the shared SQL path (same auth,
        # timeouts and cold-warehouse retry as lifecycle_databricks).
        versions: dict[tuple[str, int, int], datetime] | None = None
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fs-crosscheck-sql") as pool:
            bronze_future = pool.submit(bronze) if self.bronze_table else None
            fs_started = time.monotonic()
            columns, rows = _run(executor, query, "crosscheck feature_store")
            data = parse_crosscheck_rows(columns, rows)
            ds_log.info(
                f"crosscheck feature_store: table={self.table} machines={len(machine_ids)} "
                f"months={months[0][0]}-{months[0][1]:02d}..{months[-1][0]}-{months[-1][1]:02d} "
                f"machine_months={len(data)} in {time.monotonic() - fs_started:.1f}s"
            )
            if bronze_future is not None:
                try:
                    versions = bronze_future.result()
                    staleness["status"] = "ok"
                except Exception as exc:  # noqa: BLE001 - fail-open: keep plain comparison
                    staleness["status"] = "error"
                    staleness["error"] = f"{type(exc).__name__}: {ds_log.redact(exc)[:300]}"
                    ds_log.warning(
                        f"crosscheck staleness check failed (table={self.bronze_table}): {ds_log.error_text(exc)} "
                        "-> fell back to: plain blob-vs-feature_store comparison without stale gating"
                    )
            else:
                ds_log.warning("crosscheck staleness gate disabled (no bronze table) -> plain comparison")
        return {
            "data": data,
            "bronze_versions": versions,
            "staleness": staleness,
            "elapsed_s": round(time.monotonic() - started, 2),
            "query_ran": True,
        }


def compare_month(
    month: Any,
    fs: dict[str, Any] | None,
    target_features: list[str],
    tolerance: float,
    bronze_versions: dict[tuple[str, int, int], datetime] | None = None,
) -> dict[str, Any]:
    """Crosscheck annotation for one blob month. Never mutates status fields.

    ``bronze_versions`` None means staleness is unknown (not checked / failed):
    the plain comparison applies.
    """
    result = _compare_counts(month, fs, target_features, tolerance)
    if result["status"] == "not_checked" or bronze_versions is None or not result.get("blob_rows"):
        return result
    partition = month.partition
    key = (str(month.machine_id).lower(), partition.year, partition.month)
    blob_modified = _as_utc(getattr(month, "last_modified", None))
    bronze_modified = bronze_versions.get(key)
    result["blob_modified"] = blob_modified.isoformat() if blob_modified else None
    result["bronze_modified"] = bronze_modified.isoformat() if bronze_modified else None
    if bronze_modified is None:
        stale_reason = "Databricks has no copy of this blob file"
    elif blob_modified is not None and blob_modified > bronze_modified + _STALENESS_SLACK:
        stale_reason = "Databricks copy is older than blob"
    else:
        return result
    result["suppressed_flags"] = list(result["flags"])
    result["flags"] = ["feature_store_stale"]
    result["status"] = "feature_store_stale"
    result["stale_reason"] = stale_reason
    return result


def _compare_counts(month: Any, fs: dict[str, Any] | None, target_features: list[str], tolerance: float) -> dict[str, Any]:
    if getattr(month, "status", None) == "scan_error" or getattr(month, "error", None):
        return {"status": "not_checked", "flags": [], "detail": "blob scan error"}
    blob_rows = int(getattr(month, "row_count", None) or 0)
    fs_rows = int((fs or {}).get("rows") or 0)
    flags: list[str] = []
    result: dict[str, Any] = {"blob_rows": blob_rows, "feature_store_rows": fs_rows, "row_ratio": None}
    if blob_rows and fs_rows:
        ratio = blob_rows / fs_rows
        result["row_ratio"] = round(ratio, 3)
        if abs(ratio - 1.0) > tolerance + 1e-9:
            flags.append("row_ratio")
        v2 = [feature for feature in target_features if feature.endswith("_v2")]
        blob_counts = getattr(month, "feature_non_null_counts", None) or {}
        fs_counts = (fs or {}).get("v2") or {}
        blob_only = [f for f in v2 if blob_counts.get(f, 0) > 0 and fs_counts.get(f, 0) == 0]
        fs_only = [f for f in v2 if fs_counts.get(f, 0) > 0 and blob_counts.get(f, 0) == 0]
        if blob_only or fs_only:
            flags.append("v2_presence")
            result["v2_blob_only"] = blob_only
            result["v2_feature_store_only"] = fs_only
        result["blob_v2_non_null"] = {f: int(blob_counts.get(f, 0)) for f in v2}
        result["feature_store_v2_non_null"] = {f: int(fs_counts.get(f, 0)) for f in v2}
        result["feature_store_v1_non_null"] = dict((fs or {}).get("v1") or {})
    elif blob_rows:
        flags.append("missing_in_feature_store")
    elif fs_rows:
        flags.append("missing_in_blob")
        result["feature_store_v2_non_null"] = dict((fs or {}).get("v2") or {})
    result["flags"] = flags
    result["status"] = "mismatch" if flags else ("match" if (blob_rows or fs_rows) else "absent_both")
    return result


def apply_crosscheck(
    machines: list[Any],
    fetched: dict[str, Any],
    target_features: list[str],
    *,
    table: str,
    tolerance: float,
    ran_at: str | None = None,
) -> dict[str, Any]:
    """Annotate months/machines in place and return the run summary."""
    data = fetched.get("data") or {}
    versions = fetched.get("bronze_versions")
    staleness = dict(fetched.get("staleness") or {"status": "skipped"})
    if staleness.get("status") != "ok":
        versions = None
    flagged_months = flagged_machines = checked = 0
    stale_months = stale_with_differences = clean_months = 0
    flags_by_type: dict[str, int] = {}
    for machine in machines:
        machine_flags = machine_stale = 0
        for month in machine.months:
            key = (str(month.machine_id).lower(), month.partition.year, month.partition.month)
            annotation = compare_month(month, data.get(key), target_features, tolerance, versions)
            month.crosscheck = annotation
            status = annotation["status"]
            if status != "not_checked":
                checked += 1
            if status == "mismatch":
                machine_flags += 1
                for flag in annotation["flags"]:
                    flags_by_type[flag] = flags_by_type.get(flag, 0) + 1
            elif status == "feature_store_stale":
                machine_stale += 1
                if annotation.get("suppressed_flags"):
                    stale_with_differences += 1
            elif status == "match":
                clean_months += 1
        machine.crosscheck = {
            "status": "mismatch" if machine_flags else ("feature_store_stale" if machine_stale else "match"),
            "flagged_months": machine_flags,
            "stale_months": machine_stale,
        }
        flagged_months += machine_flags
        flagged_machines += 1 if machine_flags else 0
        stale_months += machine_stale
    return {
        "enabled": True,
        "status": "ok",
        "table": table,
        "bronze_table": staleness.get("table"),
        "row_tolerance": tolerance,
        "ran_at": ran_at or datetime.now(timezone.utc).isoformat(),
        "query_elapsed_s": fetched.get("elapsed_s"),
        "checked_months": checked,
        # flagged_* count REAL mismatches only (Databricks holds the current file version).
        "flagged_months": flagged_months,
        "real_mismatch_months": flagged_months,
        "flagged_machines": flagged_machines,
        "stale_months": stale_months,
        "stale_months_with_differences": stale_with_differences,
        "clean_months": clean_months,
        "flags_by_type": flags_by_type,
        "staleness": staleness,
        "note": NOT_JUDGED_NOTE,
        "staleness_note": STALENESS_NOTE,
    }


def error_summary(exc: BaseException, *, table: str | None, tolerance: float) -> dict[str, Any]:
    return {
        "enabled": True,
        "status": "error",
        "table": table,
        "row_tolerance": tolerance,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "error": f"{type(exc).__name__}: {ds_log.redact(exc)[:300]}",
        "note": NOT_JUDGED_NOTE,
    }


def crosscheck_warning(summary: dict[str, Any]) -> str:
    if summary.get("status") == "error":
        return (
            f"Feature Store cross-check failed and was skipped (fail-open; statuses unaffected): {summary.get('error')}"
        )
    staleness = summary.get("staleness") or {}
    if staleness.get("status") == "ok":
        stale_text = (
            f" {summary.get('stale_months', 0)} month(s) skipped as feature_store_stale "
            f"(Databricks copy older than blob; {summary.get('stale_months_with_differences', 0)} of them differ)."
        )
    else:
        stale_text = (
            f" Staleness could not be checked against {staleness.get('table') or 'the bronze table'}"
            f" ({staleness.get('error') or staleness.get('status')}); mismatches may include stale Databricks copies."
        )
    return (
        f"Feature Store cross-check vs {summary.get('table')}: {summary.get('flagged_months', 0)} real mismatched month(s) "
        f"across {summary.get('flagged_machines', 0)} machine(s) (row tolerance ±{summary.get('row_tolerance')})."
        + stale_text
        + " Informational only; statuses come from blob. "
        + NOT_JUDGED_NOTE
    )


def _month_rows(snapshot: dict[str, Any], wanted: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for machine in snapshot.get("machines") or []:
        for month in machine.get("months") or []:
            check = month.get("crosscheck") or {}
            if check.get("status") != wanted:
                continue
            partition = month.get("partition") or {}
            out.append({
                "machine_id": machine.get("machine_id"),
                "display_name": machine.get("display_name"),
                "month": f"{partition.get('year')}-{int(partition.get('month') or 0):02d}",
                "blob_status": month.get("status"),
                "activity_status": month.get("activity_status"),
                **check,
            })
    return out


def flagged_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat list of REAL mismatched machine/months from a stored snapshot dict."""
    return _month_rows(snapshot, "mismatch")


def stale_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Flat list of feature_store_stale machine/months (Databricks copy older than blob)."""
    return _month_rows(snapshot, "feature_store_stale")
