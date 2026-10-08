"""[data-source] logging: one greppable logger for which source fed a scan.

Covers the startup lines, the per-fetch lifecycle line, fallback/fail-open
warnings, the per-scan summary, the per-machine lifecycle provenance fields,
redaction, and that INFO actually reaches stderr under uvicorn's logging config.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import backfill_dashboard.scanner as scanner_module
from backfill_dashboard import data_source_log as ds_log
from backfill_dashboard.config import Settings
from backfill_dashboard.feature_store_crosscheck import DEFAULT_FEATURE_STORE_TABLE, FeatureStoreCrosscheck
from backfill_dashboard.lifecycle_databricks import DatabricksLifecycleProvider
from backfill_dashboard.models import MonthPartition
from backfill_dashboard.months import orchestrator_month_index
from backfill_dashboard.parquet_inspector import ParquetFeatureSummary
from backfill_dashboard.storage import BlobNotFoundError, BlobObject
from test_lifecycle_databricks import _row

MID = "68b0667a7e20e7c69b3c7350"
OTHER = "aaaaaaaaaaaaaaaaaaaaaaaa"
FEATURE = "ultrasonic_rms_v2"
TABLE = "dih_prod.bronze_augury_mh_mongodb.machines_raw"
WAREHOUSE = "6ed9ddd0b2661edc"


def _path(machine, y, m):
    return f"machine_id={machine}/quarter={y}Q{(m - 1) // 3 + 1}/month={m}/partition_version=last/part-0.parquet"


class Store:
    def __init__(self, machines):
        self.names = {_path(machine, 2026, 1) for machine in machines}

    def list_blob_names(self, prefix):
        return [name for name in self.names if name.startswith(prefix)]

    def read_parquet_metadata(self, name):
        if name not in self.names:
            raise BlobNotFoundError(name)
        return BlobObject(name, b"x", "https://example.test/" + name, "2026-09-28T12:13:08+00:00")

    read_blob = read_parquet_metadata


def _databricks(connect):
    return DatabricksLifecycleProvider(table=TABLE, profile="mlops-dev", warehouse_id=WAREHOUSE, connect=connect)


def _records_for(*machine_ids):
    columns = list(_row().keys())

    def connect(query):
        return columns, [tuple(_row(machine_id=machine_id).values()) for machine_id in machine_ids]

    return connect


def _scan(monkeypatch, provider, status="healthy", machines=(MID,), crosscheck=None):
    settings = Settings(scan_workers=2)
    object.__setattr__(settings, "target_features", [FEATURE])
    object.__setattr__(settings, "test_machine_ids", ())
    inventory = SimpleNamespace(list_machine_ids=lambda: list(machines))
    scanner = scanner_module.BackfillScanner(
        settings, inventory, Store(machines), SimpleNamespace(row_counts=lambda *_: {}), provider, status
    )
    if crosscheck is not None:
        scanner.feature_crosscheck = crosscheck
    monkeypatch.setattr(
        scanner, "_coverage_partitions", lambda starts: [MonthPartition(orchestrator_month_index(2026, 1), 2026, 1)]
    )
    monkeypatch.setattr(
        scanner_module, "load_fst_schema_contract",
        lambda _: SimpleNamespace(columns=("machine_id", FEATURE), schema_version="test"),
    )
    monkeypatch.setattr(
        scanner_module, "inspect_feature_partition",
        lambda content, features: ParquetFeatureSummary(10, ["machine_id", FEATURE], {FEATURE: 10}),
    )
    return scanner.scan("scan-test")


def _lines(caplog, level=None):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == ds_log.LOGGER_NAME and (level is None or record.levelno == level)
    ]


@pytest.fixture(autouse=True)
def _env(monkeypatch, caplog):
    monkeypatch.setenv("LIFECYCLE_SOURCE", "databricks")
    monkeypatch.delenv("FEATURES_CROSSCHECK", raising=False)
    caplog.set_level(logging.INFO, logger=ds_log.LOGGER_NAME)


# --- logger wiring --------------------------------------------------------------

def test_logger_is_child_of_uvicorn_error_and_prefixed(caplog):
    assert ds_log.LOGGER_NAME.startswith("uvicorn.error.")
    ds_log.info("hello")
    assert _lines(caplog) == ["[data-source] hello"]


def test_info_reaches_stderr_under_uvicorn_logging_config():
    backend = Path(__file__).resolve().parents[1]
    code = (
        "import logging.config, uvicorn.config;"
        "logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG);"
        "from backfill_dashboard import data_source_log as d;"
        "d.info('visible-check')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, timeout=60,
        env={"PYTHONPATH": str(backend), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "INFO:" in result.stderr and "[data-source] visible-check" in result.stderr


def test_redact_hides_credentials():
    text = ds_log.redact(
        "mongodb+srv://user:pw@cluster.example/db?x=1 token=abc123 password=hunter2 "
        "Authorization: Bearer eyJabc.def dapi0123456789abcdef0123"
    )
    for secret in ("user:pw", "abc123", "hunter2", "eyJabc", "dapi0123456789abcdef0123"):
        assert secret not in text
    assert "<redacted>" in text


# --- startup ---------------------------------------------------------------------

def test_startup_lines_name_every_source(monkeypatch, caplog):
    from backfill_dashboard.factory import log_startup_data_sources

    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    provider = _databricks(_records_for(MID))
    inventory = SimpleNamespace(list_machine_ids=lambda: [MID, OTHER])
    log_startup_data_sources(Settings(), inventory, provider, "healthy")

    lines = _lines(caplog, logging.INFO)
    assert (
        "[data-source] startup lifecycle: configured=databricks (LIFECYCLE_SOURCE='databricks') status=healthy "
        f"using=databricks bronze machines_raw table={TABLE} warehouse={WAREHOUSE} profile=mlops-dev"
    ) in lines
    assert any(line.startswith("[data-source] startup inventory: file:") and line.endswith("machines=2") for line in lines)
    assert any(line.startswith("[data-source] startup features: source=blob account=") for line in lines)
    assert (
        f"[data-source] startup crosscheck: on (feature_store) table={DEFAULT_FEATURE_STORE_TABLE} "
        "bronze_staleness_table=dih_prod.bronze_augury_mh_blob.feature_store"
    ) in lines
    assert not _lines(caplog, logging.WARNING)


def test_startup_warns_when_configured_provider_unavailable(caplog):
    from backfill_dashboard.factory import log_startup_data_sources

    log_startup_data_sources(Settings(), SimpleNamespace(list_machine_ids=lambda: [MID]), None, "unauthorized")
    assert "[data-source] startup crosscheck: off (FEATURES_CROSSCHECK not set to feature_store)" in _lines(caplog)
    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "databricks provider unavailable (status unauthorized)" in warnings[0]
    assert "no Mongo fallback" in warnings[0]


# --- per-fetch + summary + per-machine -------------------------------------------

def test_scan_logs_fetch_and_summary_and_tags_machines(monkeypatch, caplog):
    snapshot = _scan(monkeypatch, _databricks(_records_for(MID)), machines=(MID, OTHER))
    lines = _lines(caplog, logging.INFO)

    fetch = [line for line in lines if line.startswith("[data-source] lifecycle fetch:")]
    assert len(fetch) == 1
    assert fetch[0].startswith(
        f"[data-source] lifecycle fetch: source=databricks provider=databricks bronze machines_raw table={TABLE} "
        f"warehouse={WAREHOUSE} profile=mlops-dev requested=2 returned=1 enriched=1 duration="
    )
    assert any(line.startswith("[data-source] scan start: scan_id=scan-test lifecycle=databricks") for line in lines)
    done = [line for line in lines if line.startswith("[data-source] scan done:")]
    assert len(done) == 1
    assert "lifecycle=databricks (1/2 enriched, status healthy)" in done[0]
    assert "features=blob " in done[0] and "crosscheck=off" in done[0]
    # The machine that bronze did not return is called out as a WARNING.
    missing = _lines(caplog, logging.WARNING)
    assert len(missing) == 1 and OTHER in missing[0] and "not found in databricks" in missing[0]

    by_id = {machine.machine_id: machine for machine in snapshot.machines}
    assert by_id[MID].lifecycle_source == "databricks" and by_id[MID].lifecycle_enriched is True
    assert by_id[OTHER].lifecycle_source == "databricks" and by_id[OTHER].lifecycle_enriched is False


def test_machine_lifecycle_fields_are_serialized():
    from dataclasses import asdict

    from backfill_dashboard.models import MachineStatus

    fields = MachineStatus.__dataclass_fields__
    assert "lifecycle_source" in fields and "lifecycle_enriched" in fields
    assert fields["lifecycle_source"].default is None and fields["lifecycle_enriched"].default is False
    del asdict  # serialization goes through dataclass fields (asdict) in the API


def test_lifecycle_fetch_failure_warns_with_fallback_and_redacts(monkeypatch, caplog):
    def broken(query):
        raise RuntimeError("warehouse said no; token=supersecret123")

    snapshot = _scan(monkeypatch, _databricks(broken))
    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].startswith("[data-source] lifecycle fetch FAILED: source=databricks provider=databricks bronze")
    assert "fell back to: no lifecycle enrichment for this scan (fail-open; no Mongo fallback)" in warnings[0]
    assert "supersecret123" not in warnings[0] and "token=<redacted>" in warnings[0]
    done = [line for line in _lines(caplog) if line.startswith("[data-source] scan done:")]
    assert "lifecycle=none (source databricks, status healthy, 0/1 enriched)" in done[0]
    assert snapshot.machines[0].lifecycle_source is None and snapshot.machines[0].lifecycle_enriched is False


def test_unavailable_provider_warns_after_reconnect(monkeypatch, caplog):
    monkeypatch.setattr(scanner_module, "build_lifecycle_provider", lambda: (None, "unauthorized"))
    _scan(monkeypatch, None, status="unauthorized")
    lines = _lines(caplog)
    assert "[data-source] lifecycle reconnect: startup status was unauthorized; rebuilding databricks provider" in lines
    assert "[data-source] lifecycle reconnect result: status=unauthorized provider=none" in lines
    warnings = _lines(caplog, logging.WARNING)
    assert warnings == [
        "[data-source] lifecycle unavailable: source=databricks status=unauthorized -> fell back to: no lifecycle "
        "enrichment for this scan (no Mongo fallback)"
    ]


def test_lifecycle_off_is_info_not_warning(monkeypatch, caplog):
    monkeypatch.setenv("LIFECYCLE_SOURCE", "off")
    _scan(monkeypatch, None, status="off")
    assert "[data-source] lifecycle disabled (LIFECYCLE_SOURCE=off): scanning without lifecycle enrichment" in _lines(caplog)
    assert not _lines(caplog, logging.WARNING)


# --- SQL attempts --------------------------------------------------------------

def test_sql_attempts_logged_warm_timeout_then_cold_retry(monkeypatch, caplog):
    import time

    calls: list[float] = []

    def flaky(query, *, timeout_seconds=None):
        calls.append(float(timeout_seconds))
        if len(calls) == 1:
            time.sleep(0.2)
        return ["machine_id"], [("m1",)]

    provider = DatabricksLifecycleProvider(
        table=TABLE, profile="mlops-dev", warehouse_id="wh", timeout_seconds=0.05, cold_timeout_seconds=1.0
    )
    provider._sql_warm = True
    provider._connect = None
    monkeypatch.setattr(provider, "_execute_databricks", flaky)
    provider._execute("SELECT 1", label="lifecycle machines_raw")

    warnings = _lines(caplog, logging.WARNING)
    assert warnings == [
        "[data-source] databricks sql [lifecycle machines_raw]: attempt 1/2 (warm, budget 0.05s) timed out after "
        "0.05s -> retrying once with cold budget 1s"
    ]
    ok = [line for line in _lines(caplog, logging.INFO) if "ok in" in line]
    assert ok and ok[0].startswith(
        "[data-source] databricks sql [lifecycle machines_raw]: attempt 2/2 (cold retry, budget 1s) ok in"
    )
    assert ok[0].endswith("rows=1 warehouse=wh")
    assert provider.last_fetch_attempts[0] == "warm 0.05s timeout"
    assert provider.last_fetch_attempts[1].startswith("cold retry 1s ok ")


def test_sql_error_logged_as_warning(monkeypatch, caplog):
    provider = DatabricksLifecycleProvider(table=TABLE, profile="mlops-dev", warehouse_id="wh", timeout_seconds=1.0, cold_timeout_seconds=1.0)
    provider._connect = None

    def boom(query, *, timeout_seconds=None):
        raise RuntimeError("forced token refresh: cache update: exit status 45")

    monkeypatch.setattr(provider, "_execute_databricks", boom)
    with pytest.raises(RuntimeError):
        provider._execute("SELECT 1", label="crosscheck feature_store")
    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].startswith("[data-source] databricks sql [crosscheck feature_store]: attempt 1/1 (cold, budget 1s) failed after")
    assert "exit status 45" in warnings[0] and warnings[0].endswith("-> caller fails open")
    # Crosscheck SQL does not pollute the lifecycle attempt notes.
    assert provider.last_fetch_attempts == []


# --- crosscheck fail-open --------------------------------------------------------

class Executor:
    def __init__(self, error=None, bronze_error=None):
        self.error = error
        self.bronze_error = bronze_error

    def __call__(self, query):
        if "_source_file" in query:
            if self.bronze_error:
                raise self.bronze_error
            return ["_source_file", "bronze_modified"], []
        if self.error:
            raise self.error
        return ["machine_id", "year", "month", "row_count", f"v2__{FEATURE}", "v1__ultrasonic_rms"], [(MID, 2026, 1, 10, 10, 10)]


def test_crosscheck_failure_warns_fail_open(monkeypatch, caplog):
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    runner = FeatureStoreCrosscheck(table=DEFAULT_FEATURE_STORE_TABLE, tolerance=0.1, executor=Executor(error=RuntimeError("auth error")))
    snapshot = _scan(monkeypatch, _databricks(_records_for(MID)), crosscheck=runner)
    warnings = _lines(caplog, logging.WARNING)
    assert any(
        line.startswith(f"[data-source] crosscheck failed (table={DEFAULT_FEATURE_STORE_TABLE}): RuntimeError: auth error")
        and line.endswith("-> fell back to: no crosscheck for this scan (fail-open; statuses unaffected)")
        for line in warnings
    )
    done = [line for line in _lines(caplog) if line.startswith("[data-source] scan done:")][0]
    assert "crosscheck=feature_store error (fail-open)" in done
    assert snapshot.crosscheck["status"] == "error"


def test_crosscheck_staleness_failure_warns_and_keeps_comparison(monkeypatch, caplog):
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")
    runner = FeatureStoreCrosscheck(
        table=DEFAULT_FEATURE_STORE_TABLE, tolerance=0.1, executor=Executor(bronze_error=TimeoutError("bronze slow"))
    )
    _scan(monkeypatch, _databricks(_records_for(MID)), crosscheck=runner)
    warnings = _lines(caplog, logging.WARNING)
    assert any(
        line.startswith("[data-source] crosscheck staleness check failed (table=dih_prod.bronze_augury_mh_blob.feature_store): TimeoutError: bronze slow")
        and line.endswith("-> fell back to: plain blob-vs-feature_store comparison without stale gating")
        for line in warnings
    )
    infos = _lines(caplog, logging.INFO)
    assert any(line.startswith(f"[data-source] crosscheck feature_store: table={DEFAULT_FEATURE_STORE_TABLE} machines=1") for line in infos)
    done = [line for line in infos if line.startswith("[data-source] scan done:")][0]
    assert "crosscheck=feature_store ok (" in done and "staleness=error" in done


# --- mongo provider --------------------------------------------------------------

def test_mongo_connect_failure_warns_without_leaking_url(monkeypatch, caplog):
    import pymongo

    from backfill_dashboard.mongo_inventory import build_mongo_provider

    url = "mongodb+srv://reader:Sup3rS3cret@cluster0.example.net/production"
    monkeypatch.setenv("MONGODB_URL", url)
    monkeypatch.delenv("MONGODB_USERNAME", raising=False)
    monkeypatch.delenv("MONGODB_PASSWORD", raising=False)

    def refuse(*args, **kwargs):
        raise RuntimeError(f"Authentication failed connecting to {url}")

    monkeypatch.setattr(pymongo, "MongoClient", refuse)
    provider, status = build_mongo_provider()
    assert provider is None and status == "unauthorized"
    warnings = _lines(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].startswith(
        "[data-source] mongo lifecycle provider: connect/ping failed (credentials from env MONGODB_URL/MONGODB_URI)"
    )
    assert "Sup3rS3cret" not in warnings[0] and "reader" not in warnings[0]
    assert warnings[0].endswith("-> status unauthorized")
