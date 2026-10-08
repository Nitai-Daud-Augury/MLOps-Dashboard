"""FEATURES_CROSSCHECK=feature_store: informational blob-vs-Databricks check.

Blob stays the source of truth: the cross-check must never change a status,
must be off (no queries) by default, and must fail open.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import backfill_dashboard.scanner as scanner_module
from backfill_dashboard.config import Settings
from backfill_dashboard.feature_store_crosscheck import (
    DEFAULT_FEATURE_STORE_TABLE,
    NOT_JUDGED_NOTE,
    FeatureStoreCrosscheck,
    build_crosscheck_query,
    compare_month,
    crosscheck_enabled,
    flagged_rows,
    row_tolerance,
)
from backfill_dashboard.models import MonthPartition
from backfill_dashboard.months import orchestrator_month_index
from backfill_dashboard.parquet_inspector import ParquetFeatureSummary
from backfill_dashboard.storage import BlobNotFoundError, BlobObject

MID = "68b0667a7e20e7c69b3c7350"
V2 = "ultrasonic_rms_v2"
# Blob partitions: Jan..Apr 2026 (May has none). Mar lacks v2 on blob.
BLOB = {(2026, 1): (1000, True), (2026, 2): (3362, True), (2026, 3): (1000, False), (2026, 4): (500, True)}
CALENDAR = [(2026, m) for m in range(1, 6)]
BLOB_MODIFIED = "2026-09-28T12:13:08+00:00"
BRONZE_PREFIX = "/Volumes/dih_prod/bronze_augury_mh_blob/feature-store-production/"


def _path(y, m):
    return f"machine_id={MID}/quarter={y}Q{(m - 1) // 3 + 1}/month={m}/partition_version=last/part-0.parquet"


class Store:
    def __init__(self):
        self.by_path = {_path(y, m): (y, m) for (y, m) in BLOB}

    def list_blob_names(self, prefix):
        return [name for name in self.by_path if name.startswith(prefix)]

    def read_parquet_metadata(self, name):
        if name not in self.by_path:
            raise BlobNotFoundError(name)
        return BlobObject(name, self.by_path[name], "https://example.test/" + name, BLOB_MODIFIED)

    read_blob = read_parquet_metadata


def _summary(content, features):
    rows, has_v2 = BLOB[content]
    columns = ["machine_id", V2] if has_v2 else ["machine_id"]
    return ParquetFeatureSummary(rows, columns, {V2: rows} if has_v2 else {})


def _fs_rows():
    # (machine, year, month, row_count, v2 non-null, v1 non-null)
    return [
        (MID, 2026, 1, 1050, 1050, 1050),   # ratio 0.952 -> within 10%
        (MID, 2026, 2, 1448, 1448, 1448),   # ratio 2.32 -> row_ratio
        (MID, 2026, 3, 1000, 1000, 1000),   # blob lacks v2 -> v2_presence
        # 2026-04 absent in feature_store -> missing_in_feature_store
        (MID, 2026, 5, 700, 700, 700),      # no blob partition -> missing_in_blob
    ]


def _bronze_rows(overrides=None, missing=()):
    """Bronze copy of every blob file at the same version unless overridden."""
    overrides = overrides or {}
    rows = []
    for (y, m) in BLOB:
        if (y, m) in missing:
            continue
        rows.append((BRONZE_PREFIX + _path(y, m), overrides.get((y, m), BLOB_MODIFIED.replace("T", " "))))
    return rows


class FakeExecutor:
    def __init__(self, rows=None, error=None, bronze_rows=None, bronze_error=None):
        self.queries: list[str] = []
        self.bronze_queries: list[str] = []
        self.rows = rows if rows is not None else _fs_rows()
        self.bronze_rows = bronze_rows if bronze_rows is not None else _bronze_rows()
        self.error = error
        self.bronze_error = bronze_error

    def __call__(self, query):
        if "_source_file" in query:
            self.bronze_queries.append(query)
            if self.bronze_error:
                raise self.bronze_error
            return ["_source_file", "bronze_modified"], self.bronze_rows
        self.queries.append(query)
        if self.error:
            raise self.error
        return ["machine_id", "year", "month", "row_count", f"v2__{V2}", "v1__ultrasonic_rms"], self.rows


def _scan(monkeypatch, executor=None):
    settings = Settings(scan_workers=2)
    object.__setattr__(settings, "target_features", [V2])
    object.__setattr__(settings, "test_machine_ids", ())
    inventory = SimpleNamespace(list_machine_ids=lambda: [MID])
    scanner = scanner_module.BackfillScanner(settings, inventory, Store(), SimpleNamespace(row_counts=lambda *_: {}), None, "off")
    if executor is not None:
        scanner.feature_crosscheck = FeatureStoreCrosscheck(table=DEFAULT_FEATURE_STORE_TABLE, tolerance=row_tolerance(), executor=executor)
    monkeypatch.setattr(
        scanner, "_coverage_partitions",
        lambda starts: [MonthPartition(orchestrator_month_index(y, m), y, m) for y, m in CALENDAR],
    )
    monkeypatch.setattr(scanner_module, "load_fst_schema_contract", lambda _: SimpleNamespace(columns=("machine_id", V2), schema_version="test"))
    monkeypatch.setattr(scanner_module, "inspect_feature_partition", _summary)
    return scanner.scan("cx")


def _statuses(snapshot):
    return {
        (mo.partition.year, mo.partition.month): (mo.status, mo.activity_status, mo.reason, mo.row_count)
        for machine in snapshot.machines for mo in machine.months
    } | {("machine", m.machine_id): (m.status, m.months_complete, m.months_expected) for m in snapshot.machines}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("FEATURES_CROSSCHECK", "FEATURES_CROSSCHECK_ROW_TOLERANCE", "DATABRICKS_FEATURE_STORE_TABLE", "FEATURES_CROSSCHECK_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)


# --- off by default ------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "off", "0", "blob"])
def test_off_by_default_runs_no_query(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("FEATURES_CROSSCHECK", value)
    executor = FakeExecutor()
    snapshot = _scan(monkeypatch, executor)
    assert not crosscheck_enabled()
    assert executor.queries == [] and executor.bronze_queries == []
    assert snapshot.crosscheck is None
    assert all(mo.crosscheck is None for m in snapshot.machines for mo in m.months)
    assert not any("cross-check" in warning for warning in snapshot.warnings)


def test_off_does_not_build_a_default_runner(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("must not build a crosscheck runner when off")

    monkeypatch.setattr(scanner_module, "FeatureStoreCrosscheck", boom)
    assert _scan(monkeypatch).crosscheck is None


# --- mismatch flagging ---------------------------------------------------------

def test_mismatches_are_flagged_per_month(monkeypatch):
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    executor = FakeExecutor()
    snapshot = _scan(monkeypatch, executor)
    assert len(executor.queries) == 1 and len(executor.bronze_queries) == 1
    checks = {(mo.partition.year, mo.partition.month): mo.crosscheck for mo in snapshot.machines[0].months}

    assert checks[(2026, 1)]["status"] == "match"
    assert checks[(2026, 2)]["flags"] == ["row_ratio"]
    assert checks[(2026, 2)]["row_ratio"] == pytest.approx(2.322, abs=1e-3)
    assert checks[(2026, 2)]["blob_rows"] == 3362 and checks[(2026, 2)]["feature_store_rows"] == 1448
    assert checks[(2026, 3)]["flags"] == ["v2_presence"]
    assert checks[(2026, 3)]["v2_feature_store_only"] == [V2]
    assert checks[(2026, 4)]["flags"] == ["missing_in_feature_store"]
    assert checks[(2026, 5)]["flags"] == ["missing_in_blob"]

    summary = snapshot.crosscheck
    assert summary["status"] == "ok"
    assert summary["table"] == DEFAULT_FEATURE_STORE_TABLE
    assert summary["flagged_months"] == 4 and summary["flagged_machines"] == 1
    assert summary["flags_by_type"] == {"row_ratio": 1, "v2_presence": 1, "missing_in_feature_store": 1, "missing_in_blob": 1}
    assert "23 truly absent" in summary["note"] and "11 case-duplicate" in summary["note"]
    assert summary["staleness"]["status"] == "ok" and summary["stale_months"] == 0
    assert summary["real_mismatch_months"] == 4 and summary["clean_months"] == 1
    assert snapshot.machines[0].crosscheck == {"status": "mismatch", "flagged_months": 4, "stale_months": 0}
    assert any("Feature Store cross-check" in w and "23 truly absent" in w for w in snapshot.warnings)
    # Same bronze version -> timestamps recorded, not stale.
    assert checks[(2026, 2)]["blob_modified"] == checks[(2026, 2)]["bronze_modified"] == BLOB_MODIFIED


def test_statuses_identical_with_and_without_crosscheck(monkeypatch):
    baseline = _statuses(_scan(monkeypatch))
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    assert _statuses(_scan(monkeypatch, FakeExecutor())) == baseline


def test_query_pushes_filters_down_and_aggregates():
    query = build_crosscheck_query(DEFAULT_FEATURE_STORE_TABLE, [MID, "abc'd"], (2025, 11), (2026, 2), [V2, "f1"])
    assert "SELECT *" not in query.upper()
    assert f"machine_id IN ('{MID}', 'abc''d')" in query or f"machine_id IN ('abc''d', '{MID}')" in query
    assert "recorded_at >= TIMESTAMP '2025-11-01 00:00:00'" in query
    assert "recorded_at < TIMESTAMP '2026-03-01 00:00:00'" in query
    assert f"COUNT(`{V2}`) AS `v2__{V2}`" in query
    assert "COUNT(`ultrasonic_rms`) AS `v1__ultrasonic_rms`" in query
    assert "GROUP BY machine_id" in query


# --- tolerance -----------------------------------------------------------------

def _month(rows, v2=True):
    return SimpleNamespace(status="backfilled", error=None, row_count=rows, feature_non_null_counts={V2: rows} if v2 else {})


def test_row_tolerance_boundaries():
    fs = {"rows": 1000, "v2": {V2: 1000}, "v1": {}}
    assert compare_month(_month(1100), fs, [V2], 0.10)["status"] == "match"
    assert compare_month(_month(1101), fs, [V2], 0.10)["flags"] == ["row_ratio"]
    assert compare_month(_month(900), fs, [V2], 0.10)["status"] == "match"
    assert compare_month(_month(1080), fs, [V2], 0.05)["flags"] == ["row_ratio"]


def test_row_tolerance_env(monkeypatch):
    assert row_tolerance() == 0.10
    monkeypatch.setenv("FEATURES_CROSSCHECK_ROW_TOLERANCE", "0.25")
    assert row_tolerance() == 0.25
    monkeypatch.setenv("FEATURES_CROSSCHECK_ROW_TOLERANCE", "abc")
    assert row_tolerance() == 0.10
    monkeypatch.setenv("FEATURES_CROSSCHECK_ROW_TOLERANCE", "-1")
    assert row_tolerance() == 0.10


def test_tolerance_changes_flags_in_scan(monkeypatch):
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    monkeypatch.setenv("FEATURES_CROSSCHECK_ROW_TOLERANCE", "0.01")
    snapshot = _scan(monkeypatch, FakeExecutor())
    jan = next(mo for mo in snapshot.machines[0].months if mo.partition.month == 1)
    assert jan.crosscheck["flags"] == ["row_ratio"]  # 1000/1050 outside ±1%


def test_scan_error_month_is_not_checked():
    month = SimpleNamespace(status="scan_error", error="boom", row_count=None, feature_non_null_counts={})
    assert compare_month(month, {"rows": 10}, [V2], 0.1)["status"] == "not_checked"


# --- fail-open -----------------------------------------------------------------

def test_fail_open_on_query_error(monkeypatch):
    baseline = _statuses(_scan(monkeypatch))
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    snapshot = _scan(monkeypatch, FakeExecutor(error=RuntimeError("warehouse asleep")))
    assert _statuses(snapshot) == baseline
    assert snapshot.crosscheck["status"] == "error"
    assert "warehouse asleep" in snapshot.crosscheck["error"]
    assert all(mo.crosscheck is None for m in snapshot.machines for mo in m.months)
    assert any("fail-open" in w for w in snapshot.warnings)


def test_fail_open_when_runner_cannot_be_built(monkeypatch):
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")

    def broken(**_kwargs):
        raise RuntimeError("no databricks deps")

    monkeypatch.setattr(scanner_module, "FeatureStoreCrosscheck", broken)
    snapshot = _scan(monkeypatch)
    assert snapshot.crosscheck["status"] == "error"
    assert snapshot.machines[0].status in {"needs_backfill", "backfilled"}


def test_fail_open_on_timeout(monkeypatch):
    import threading

    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    monkeypatch.setenv("FEATURES_CROSSCHECK_TIMEOUT_SECONDS", "0.2")
    release = threading.Event()

    def slow(query):
        release.wait(5)
        return [], []

    try:
        snapshot = _scan(monkeypatch, slow)
    finally:
        release.set()
    assert snapshot.crosscheck["status"] == "error"
    assert "TimeoutError" in snapshot.crosscheck["error"]


# --- data_sources / startup log / API -----------------------------------------

def test_data_sources_crosscheck_off(monkeypatch):
    from backfill_dashboard.data_sources import describe_data_sources, format_data_sources_log

    info = describe_data_sources(Settings(), None, "off", None)
    block = info["features_crosscheck"]
    assert block["enabled"] is False and block["mode"] == "off"
    assert block["table"] == DEFAULT_FEATURE_STORE_TABLE
    assert block["last_run"] is None
    assert "features_crosscheck=off" in format_data_sources_log(info)


def test_data_sources_crosscheck_on_with_last_run(monkeypatch):
    from backfill_dashboard.data_sources import describe_data_sources, format_data_sources_log

    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    monkeypatch.setenv("DATABRICKS_FEATURE_STORE_TABLE", "dih_dev.silver_mh.feature_store")
    summary = {"status": "ok", "ran_at": "2026-10-08T10:00:00+00:00", "flagged_months": 2, "flagged_machines": 1, "note": NOT_JUDGED_NOTE}
    info = describe_data_sources(Settings(), None, "off", None, last_crosscheck=summary)
    block = info["features_crosscheck"]
    assert block["enabled"] is True
    assert block["table"] == "dih_dev.silver_mh.feature_store"
    assert block["row_tolerance"] == 0.10
    assert block["last_run"]["status"] == "ok" and block["last_run"]["flagged_months"] == 2
    line = format_data_sources_log(info)
    assert "features_crosscheck=feature_store" in line
    assert "feature_store_table=dih_dev.silver_mh.feature_store" in line


def test_data_sources_reports_bad_config(monkeypatch):
    from backfill_dashboard.data_sources import describe_data_sources

    monkeypatch.setenv("FEATURES_CROSSCHECK", "silver")
    monkeypatch.setenv("DATABRICKS_FEATURE_STORE_TABLE", "bad table; drop")
    block = describe_data_sources(Settings(), None, "off", None)["features_crosscheck"]
    assert block["enabled"] is False
    assert block["table"] is None and "config_error" in block


def test_startup_log_includes_crosscheck(monkeypatch, caplog):
    import backfill_dashboard.factory as factory

    monkeypatch.setenv("LIFECYCLE_SOURCE", "off")
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    monkeypatch.setattr(factory, "build_lifecycle_provider", lambda: (None, "off"))
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    factory.build_components()
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("data_sources:")]
    assert len(lines) == 1
    assert "features_crosscheck=feature_store" in lines[0]
    assert f"feature_store_table={DEFAULT_FEATURE_STORE_TABLE}" in lines[0]


def test_status_and_crosscheck_endpoints(monkeypatch):
    from backfill_dashboard import app as app_module

    monkeypatch.setenv("LIFECYCLE_SOURCE", "off")
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    month = {
        "partition": {"index": 1, "year": 2026, "month": 6},
        "status": "backfilled", "activity_status": "online",
        "crosscheck": {"status": "mismatch", "flags": ["row_ratio"], "blob_rows": 3362, "feature_store_rows": 1448, "row_ratio": 2.322},
    }
    ok_month = {"partition": {"index": 2, "year": 2026, "month": 7}, "status": "backfilled", "crosscheck": {"status": "match", "flags": []}}
    snapshot = {
        "scan_id": "s1",
        "machines": [{"machine_id": MID, "display_name": "PA30", "months": [month, ok_month]}],
        "crosscheck": {"status": "ok", "flagged_months": 1, "flagged_machines": 1},
    }
    monkeypatch.setattr(app_module.repository, "latest", lambda: {"scan_state": {}, "snapshot": snapshot})

    status = app_module.backfill_status()
    assert status["data_sources"]["features_crosscheck"]["enabled"] is True
    assert status["data_sources"]["features_crosscheck"]["last_run"]["flagged_months"] == 1

    payload = app_module.backfill_crosscheck()
    assert payload["scan_id"] == "s1"
    assert payload["summary"]["flagged_months"] == 1
    assert payload["flagged"] == flagged_rows(snapshot)
    assert len(payload["flagged"]) == 1
    row = payload["flagged"][0]
    assert row["month"] == "2026-06" and row["blob_status"] == "backfilled" and row["row_ratio"] == 2.322
    assert "23 truly absent" in payload["note"]


# --- silver default --------------------------------------------------------------

def test_silver_default_table_is_feature_store():
    import os

    if os.getenv("DATABRICKS_SILVER_TABLE"):
        pytest.skip("DATABRICKS_SILVER_TABLE overridden in this environment")
    assert Settings().silver_table == "dih_prod.silver_mh.feature_store"
    assert Settings.__dataclass_fields__["silver_table"].default == "dih_prod.silver_mh.feature_store"


def test_silver_query_uses_columns_present_in_feature_store(monkeypatch):
    """silver.py only needs machine_id + recorded_at, both present in feature_store."""
    import inspect

    from backfill_dashboard import silver

    source = inspect.getsource(silver.DatabricksSilverProvider.row_counts)
    assert "machine_id" in source and "recorded_at" in source
    assert "silver_mh.features" not in inspect.getsource(silver)


# --- staleness gate ---------------------------------------------------------------

from backfill_dashboard.feature_store_crosscheck import (  # noqa: E402
    DEFAULT_BRONZE_TABLE,
    build_bronze_versions_query,
    parse_bronze_versions,
    parse_source_file,
    stale_rows,
)


def _checks(snapshot):
    return {(mo.partition.year, mo.partition.month): mo.crosscheck for mo in snapshot.machines[0].months}


def test_blob_newer_than_bronze_becomes_stale(monkeypatch):
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    executor = FakeExecutor(bronze_rows=_bronze_rows({(2026, 2): "2026-07-05 07:51:56", (2026, 1): "2026-07-05 07:51:56"}))
    snapshot = _scan(monkeypatch, executor)
    checks = _checks(snapshot)
    feb = checks[(2026, 2)]
    assert feb["status"] == "feature_store_stale" and feb["flags"] == ["feature_store_stale"]
    assert feb["suppressed_flags"] == ["row_ratio"]
    assert feb["stale_reason"] == "Databricks copy is older than blob"
    assert feb["blob_modified"] == BLOB_MODIFIED and feb["bronze_modified"] == "2026-07-05T07:51:56+00:00"
    assert feb["row_ratio"] == pytest.approx(2.322, abs=1e-3)  # numbers kept for context
    jan = checks[(2026, 1)]
    assert jan["status"] == "feature_store_stale" and jan["suppressed_flags"] == []
    # Real mismatches stay flagged where bronze has the current version.
    assert checks[(2026, 3)]["status"] == "mismatch" and checks[(2026, 3)]["flags"] == ["v2_presence"]
    summary = snapshot.crosscheck
    assert summary["stale_months"] == 2 and summary["stale_months_with_differences"] == 1
    assert summary["real_mismatch_months"] == 3 and summary["flagged_months"] == 3
    assert "row_ratio" not in summary["flags_by_type"]
    assert summary["clean_months"] == 0
    assert snapshot.machines[0].crosscheck == {"status": "mismatch", "flagged_months": 3, "stale_months": 2}
    assert any("2 month(s) skipped as feature_store_stale" in w for w in snapshot.warnings)


def test_same_version_within_slack_is_not_stale():
    month = SimpleNamespace(status="backfilled", error=None, row_count=3362, feature_non_null_counts={V2: 3362},
                            machine_id=MID, partition=MonthPartition(1, 2026, 6), last_modified="2026-09-28T12:13:08.400000+00:00")
    from datetime import datetime, timezone
    versions = {(MID, 2026, 6): datetime(2026, 9, 28, 12, 13, 8, tzinfo=timezone.utc)}
    result = compare_month(month, {"rows": 1448, "v2": {V2: 1448}}, [V2], 0.1, versions)
    assert result["status"] == "mismatch" and result["flags"] == ["row_ratio"]
    month.last_modified = "2026-09-28T12:13:10+00:00"
    assert compare_month(month, {"rows": 1448, "v2": {V2: 1448}}, [V2], 0.1, versions)["status"] == "feature_store_stale"


def test_missing_bronze_version_is_stale(monkeypatch):
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    snapshot = _scan(monkeypatch, FakeExecutor(bronze_rows=_bronze_rows(missing={(2026, 4)})))
    apr = _checks(snapshot)[(2026, 4)]
    assert apr["status"] == "feature_store_stale"
    assert apr["stale_reason"] == "Databricks has no copy of this blob file"
    assert apr["suppressed_flags"] == ["missing_in_feature_store"] and apr["bronze_modified"] is None
    # No blob partition -> staleness does not apply; missing_in_blob stays a real flag.
    assert _checks(snapshot)[(2026, 5)]["flags"] == ["missing_in_blob"]


def test_unknown_blob_modified_is_not_stale():
    month = SimpleNamespace(status="backfilled", error=None, row_count=100, feature_non_null_counts={V2: 100},
                            machine_id=MID, partition=MonthPartition(1, 2026, 6), last_modified=None)
    from datetime import datetime, timezone
    versions = {(MID, 2026, 6): datetime(2026, 1, 1, tzinfo=timezone.utc)}
    assert compare_month(month, {"rows": 100, "v2": {V2: 100}}, [V2], 0.1, versions)["status"] == "match"
    # bronze_versions None (not checked) -> plain comparison
    assert "blob_modified" not in compare_month(month, {"rows": 100, "v2": {V2: 100}}, [V2], 0.1, None)


def test_staleness_query_failure_keeps_plain_flags(monkeypatch):
    baseline = _statuses(_scan(monkeypatch))
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    executor = FakeExecutor(bronze_error=RuntimeError("bronze denied"),
                            bronze_rows=_bronze_rows({(2026, 2): "2020-01-01 00:00:00"}))
    snapshot = _scan(monkeypatch, executor)
    assert _statuses(snapshot) == baseline
    summary = snapshot.crosscheck
    assert summary["status"] == "ok"
    assert summary["staleness"]["status"] == "error" and "bronze denied" in summary["staleness"]["error"]
    assert summary["stale_months"] == 0 and summary["real_mismatch_months"] == 4
    assert _checks(snapshot)[(2026, 2)]["flags"] == ["row_ratio"]
    assert any("Staleness could not be checked" in w for w in snapshot.warnings)


def test_statuses_identical_with_staleness_gate(monkeypatch):
    baseline = _statuses(_scan(monkeypatch))
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    stale = FakeExecutor(bronze_rows=_bronze_rows({k: "2020-01-01 00:00:00" for k in BLOB}))
    snapshot = _scan(monkeypatch, stale)
    assert _statuses(snapshot) == baseline
    assert snapshot.crosscheck["stale_months"] == 4


@pytest.mark.parametrize("path,expected", [
    (BRONZE_PREFIX + f"machine_id={MID}/quarter=2026Q2/month=5/partition_version=last/part-0.parquet", (MID, 2026, 5)),
    (f"machine_id={MID.upper()}/quarter=2025Q4/month=12/partition_version=last/part-0.parquet", (MID, 2025, 12)),
    ("C:\\x\\machine_id=abc\\quarter=2026Q1\\month=1\\partition_version=last\\part-0.parquet", ("abc", 2026, 1)),
    (f"machine_id={MID}/quarter=2026Q2/month=5/partition_version=v3/part-0.parquet", None),
    (f"machine_id={MID}/quarter=2026Q2/month=5/partition_version=last/part-1.parquet", None),
    (f"machine_id={MID}/quarter=2026Q2/month=7/partition_version=last/part-0.parquet", None),  # quarter/month mismatch
    ("", None),
    (None, None),
])
def test_parse_source_file(path, expected):
    assert parse_source_file(path) == expected


def test_parse_bronze_versions_keeps_latest_and_normalizes_utc():
    from datetime import datetime, timezone
    p = BRONZE_PREFIX + _path(2026, 6)
    versions = parse_bronze_versions(["_source_file", "bronze_modified"], [
        (p, datetime(2026, 7, 5, 7, 51, 56)),               # naive -> UTC
        (p, "2026-08-01T00:18:41+00:00"),
        ("garbage", "2026-08-01T00:00:00Z"),
        (BRONZE_PREFIX + _path(2026, 7), None),
    ])
    assert versions == {(MID, 2026, 6): datetime(2026, 8, 1, 0, 18, 41, tzinfo=timezone.utc)}


def test_bronze_query_pushes_machine_filter():
    query = build_bronze_versions_query(DEFAULT_BRONZE_TABLE, [MID, "a'b"])
    assert query.startswith("SELECT _source_file, max(_file_modification_time)")
    assert f"FROM {DEFAULT_BRONZE_TABLE}" in query
    assert "machine_id IN (" in query and "'a''b'" in query
    assert "GROUP BY _source_file" in query and "SELECT *" not in query.upper()


def test_bronze_table_env_and_validation(monkeypatch):
    from backfill_dashboard.feature_store_crosscheck import bronze_table, describe_crosscheck_config

    assert bronze_table() == DEFAULT_BRONZE_TABLE
    monkeypatch.setenv("DATABRICKS_FEATURE_STORE_BRONZE_TABLE", "dih_dev.bronze_x.feature_store")
    assert describe_crosscheck_config()["bronze_table"] == "dih_dev.bronze_x.feature_store"
    monkeypatch.setenv("DATABRICKS_FEATURE_STORE_BRONZE_TABLE", "bad; drop")
    info = describe_crosscheck_config()
    assert info["bronze_table"] is None and "config_error" in info


def test_api_and_data_sources_expose_staleness(monkeypatch):
    from backfill_dashboard import app as app_module
    from backfill_dashboard.data_sources import describe_data_sources, format_data_sources_log

    monkeypatch.setenv("LIFECYCLE_SOURCE", "off")
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    stale_month = {
        "partition": {"index": 1, "year": 2026, "month": 6}, "status": "backfilled",
        "crosscheck": {"status": "feature_store_stale", "flags": ["feature_store_stale"], "suppressed_flags": ["row_ratio"],
                       "blob_modified": BLOB_MODIFIED, "bronze_modified": "2026-07-05T07:51:56+00:00",
                       "stale_reason": "Databricks copy is older than blob"},
    }
    stale_clean = {"partition": {"index": 2, "year": 2026, "month": 5}, "status": "backfilled",
                   "crosscheck": {"status": "feature_store_stale", "flags": ["feature_store_stale"], "suppressed_flags": []}}
    real = {"partition": {"index": 3, "year": 2026, "month": 8}, "status": "backfilled",
            "crosscheck": {"status": "mismatch", "flags": ["row_ratio"], "row_ratio": 1.5}}
    summary = {"status": "ok", "flagged_months": 1, "real_mismatch_months": 1, "stale_months": 2,
               "stale_months_with_differences": 1, "clean_months": 7, "staleness": {"status": "ok", "table": DEFAULT_BRONZE_TABLE}}
    snapshot = {"scan_id": "s2", "machines": [{"machine_id": MID, "months": [stale_month, stale_clean, real]}], "crosscheck": summary}
    monkeypatch.setattr(app_module.repository, "latest", lambda: {"scan_state": {}, "snapshot": snapshot})

    payload = app_module.backfill_crosscheck()
    assert payload["counts"] == {"real_mismatch_months": 1, "stale_months": 2, "stale_months_with_differences": 1,
                                 "clean_months": 7, "staleness_status": "ok"}
    assert [row["month"] for row in payload["flagged"]] == ["2026-08"]
    assert [row["month"] for row in payload["stale"]] == ["2026-06", "2026-05"]
    assert payload["stale"] == stale_rows(snapshot)
    assert payload["stale"][0]["bronze_modified"] == "2026-07-05T07:51:56+00:00"
    assert "current and previous month" in payload["staleness_note"]

    block = app_module.backfill_status()["data_sources"]["features_crosscheck"]
    assert block["bronze_table"] == DEFAULT_BRONZE_TABLE
    assert block["last_run"]["staleness_status"] == "ok" and block["last_run"]["stale_months"] == 2
    line = format_data_sources_log(describe_data_sources(Settings(), None, "off", None))
    assert f"feature_store_bronze_table={DEFAULT_BRONZE_TABLE}" in line and "crosscheck_staleness_gate=on" in line
