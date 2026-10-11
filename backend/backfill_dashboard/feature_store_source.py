"""Databricks reader for ``FEATURES_SOURCE=databricks``: month coverage from feature_store.

Instead of listing and reading FST Parquet partitions in Azure Blob, one
aggregate query per scan on ``DATABRICKS_FEATURE_STORE_TABLE`` (default
``dih_prod.silver_mh.feature_store``) returns, per machine and calendar month
(``recorded_at``), the row count, the non-null count of every target feature
and, for each ``*_v2`` target with a v1 column, the rows where v1 is set but v2
is null (plus their first/last ``recorded_at``). Those are exactly the inputs
``BackfillDecisionPolicy.classify`` uses on a Parquet footer, so "needs backfill"
means the same thing in both modes:

* a month with rows whose target feature is absent or all null, or whose v2
  value is missing on rows that have v1          -> needs_backfill
* a month without rows after the machine's first data month -> missing
  partition (same lifecycle / installation rules as blob)
* otherwise                                         -> backfilled

It runs on the same SQL path as lifecycle and the cross-check (shared SDK
Config, serialized token refresh, cold/steady timeouts and one cold retry) via
``feature_store_crosscheck._default_executor``, plus an overall ceiling
(``FEATURES_SOURCE_TIMEOUT_SECONDS``, default 300 s). Fail-open: if the query
fails, the scan still completes and every month is ``scan_error`` with the
reason; no gap is invented.

Known limits (reported, never turned into gaps):

* Schema completeness is not available: against the canonical FST contract
  feature_store has 23 truly absent columns and collapses 11 case-duplicate
  pairs, so the per-month canonical column check is skipped.
* Staleness: bronze only re-ingests the current and previous month. A second
  query on the bronze mirror returns the blob file version Databricks holds per
  month (``_file_modification_time``, reported as ``last_modified`` with
  ``last_modified_source=databricks_bronze``); older months are flagged
  ``stale_risk``. Blob is never read in this mode.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Any, Iterable

from . import data_source_log as ds_log
from .feature_store_crosscheck import (
    _default_executor,
    _next_month,
    _quote,
    _run,
    _v1_name,
    bronze_table,
    build_bronze_versions_query,
    feature_store_table,
    parse_bronze_versions,
)
from .parquet_inspector import ParquetFeatureSummary

DEFAULT_TIMEOUT_SECONDS = 300.0


def source_timeout_seconds() -> float:
    raw = (os.getenv("FEATURES_SOURCE_TIMEOUT_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def _alias(prefix: str, feature: str) -> str:
    return f"`{prefix}__{feature}`"


def build_columns_query(table: str) -> str:
    return f"SELECT * FROM {table} LIMIT 0"


def build_coverage_query(
    table: str,
    machine_ids: Iterable[str],
    start: tuple[int, int],
    end: tuple[int, int],
    features: Iterable[str],
    gap_pairs: dict[str, str],
) -> str:
    """Per machine/month aggregates; machine_id IN + recorded_at range are pushed down
    (same shape as the cross-check query)."""
    ids = sorted({str(machine_id) for machine_id in machine_ids})
    if not ids:
        raise ValueError("no machine ids")
    aggregates = ["COUNT(*) AS row_count"]
    aggregates += [f"COUNT(`{feature}`) AS {_alias('nn', feature)}" for feature in features]
    for v2, v1 in gap_pairs.items():
        condition = f"`{v1}` IS NOT NULL AND `{v2}` IS NULL"
        aggregates.append(f"SUM(CASE WHEN {condition} THEN 1 ELSE 0 END) AS {_alias('gap', v2)}")
        aggregates.append(f"MIN(CASE WHEN {condition} THEN recorded_at END) AS {_alias('gapmin', v2)}")
        aggregates.append(f"MAX(CASE WHEN {condition} THEN recorded_at END) AS {_alias('gapmax', v2)}")
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


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def parse_coverage_rows(
    columns: list[str], rows: list[tuple[Any, ...]], features: list[str], gap_pairs: dict[str, str]
) -> dict[tuple[str, int, int], dict[str, Any]]:
    result: dict[tuple[str, int, int], dict[str, Any]] = {}
    for row in rows:
        record = dict(zip(columns, row))
        key = (str(record["machine_id"]).lower(), int(record["year"]), int(record["month"]))
        gaps: dict[str, dict[str, int | str]] = {}
        for v2 in gap_pairs:
            missing = int(record.get(f"gap__{v2}") or 0)
            if missing:
                gap: dict[str, int | str] = {"missing_rows": missing}
                first, last = _text(record.get(f"gapmin__{v2}")), _text(record.get(f"gapmax__{v2}"))
                if first:
                    gap["first_missing_at"] = first
                if last:
                    gap["last_missing_at"] = last
                gaps[v2] = gap
        result[key] = {
            "rows": int(record.get("row_count") or 0),
            "non_null": {feature: int(record.get(f"nn__{feature}") or 0) for feature in features},
            "gaps": gaps,
        }
    return result


def month_summary(entry: dict[str, Any], present_targets: list[str]) -> ParquetFeatureSummary:
    """A ParquetFeatureSummary built from SQL aggregates (target columns only)."""
    return ParquetFeatureSummary(
        row_count=int(entry.get("rows") or 0),
        columns=list(present_targets),
        feature_non_null_counts=dict(entry.get("non_null") or {}),
        feature_coverage_gaps=dict(entry.get("gaps") or {}),
    )


class FeatureStoreSource:
    """Fetches everything a databricks-mode scan needs in (at most) three SQL statements."""

    def __init__(self, *, table: str | None = None, bronze: str | None = "default", executor: Any = None,
                 lifecycle_inventory: Any = None, timeout_seconds: float | None = None) -> None:
        self.table = table or feature_store_table()
        self.bronze_table = bronze_table() if bronze == "default" else bronze
        self.executor = executor
        self.lifecycle_inventory = lifecycle_inventory
        self.timeout_seconds = timeout_seconds or source_timeout_seconds()

    def fetch(self, machine_ids: list[str], start: tuple[int, int], end: tuple[int, int],
              target_features: list[str]) -> dict[str, Any]:
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fs-source")
        try:
            future = pool.submit(self._fetch, machine_ids, start, end, target_features, pool)
            try:
                return future.result(timeout=self.timeout_seconds)
            except FutureTimeout as exc:
                raise TimeoutError(f"feature_store coverage query exceeded {self.timeout_seconds:g}s") from exc
        finally:
            pool.shutdown(wait=False)

    def _fetch(self, machine_ids, start, end, target_features, pool) -> dict[str, Any]:
        started = time.monotonic()
        executor = self.executor or _default_executor(self.lifecycle_inventory)
        columns, _ = _run(executor, build_columns_query(self.table), "features_source columns")
        by_lower = {str(name).lower(): str(name) for name in columns}
        present = [feature for feature in target_features if feature.lower() in by_lower]
        absent = [feature for feature in target_features if feature.lower() not in by_lower]
        gap_pairs = {
            feature: v1 for feature in present
            if (v1 := _v1_name(feature)) and v1.lower() in by_lower
        }
        bronze_future = None
        if self.bronze_table:
            bronze_future = pool.submit(
                _run, executor, build_bronze_versions_query(self.bronze_table, machine_ids), "features_source bronze versions"
            )
        query = build_coverage_query(self.table, machine_ids, start, end, present, gap_pairs)
        cov_columns, rows = _run(executor, query, "features_source coverage")
        data = parse_coverage_rows(cov_columns, rows, present, gap_pairs)
        versions, staleness = None, {"status": "skipped", "table": self.bronze_table}
        if bronze_future is not None:
            try:
                b_columns, b_rows = bronze_future.result(timeout=self.timeout_seconds)
                versions = parse_bronze_versions(b_columns, b_rows)
                staleness = {"status": "ok", "table": self.bronze_table, "files": len(b_rows), "mapped": len(versions)}
            except Exception as exc:  # noqa: BLE001 - versions are informational
                staleness = {"status": "error", "table": self.bronze_table, "error": ds_log.error_text(exc, 200)}
                ds_log.warning(f"features_source bronze versions failed: {ds_log.error_text(exc)} -> months carry no copy timestamp")
        elapsed = round(time.monotonic() - started, 2)
        ds_log.info(
            f"features_source databricks: table={self.table} machines={len(machine_ids)} "
            f"months={start[0]}-{start[1]:02d}..{end[0]}-{end[1]:02d} machine_months={len(data)} "
            f"targets_present={len(present)}/{len(target_features)} gap_pairs={len(gap_pairs)} "
            f"bronze={staleness['status']} in {elapsed:.1f}s"
        )
        return {
            "data": data,
            "present_targets": present,
            "absent_targets": absent,
            "gap_pairs": gap_pairs,
            "bronze_versions": versions,
            "staleness": staleness,
            "elapsed_s": elapsed,
        }
