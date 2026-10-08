"""Data-start parity: Mongo and Databricks lifecycle sources must yield the same
backfill window and gaps (machine 68b0667a7e20e7c69b3c7350, 2026-10 incident).

Facts mirrored from production (2026-10-08):
- FST partitions exist 2025-09 .. 2026-10 (no 2025-08 partition).
- 2025-09 .. 2026-03 lack the v2 target features; 2026-04/05 miss canonical
  schema columns; 2026-06 .. 2026-10 are complete.
- Bronze machines_raw: created_at 2025-08-28, firstRecorded 2025-09-03.
- Mongo: same firstRecorded, but every endpoint installation_date is 2026-06-29
  (sensor swap), which previously hid nine months of real data.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from backfill_dashboard.inventory_provider import _record
from backfill_dashboard.lifecycle_databricks import _normalize_machines_raw_row
from backfill_dashboard.models import MonthPartition
from backfill_dashboard.months import orchestrator_month_index
from backfill_dashboard.parquet_inspector import ParquetFeatureSummary
from backfill_dashboard.policy import BackfillDecisionPolicy
from backfill_dashboard.scanner import BackfillScanner
from backfill_dashboard.storage import BlobNotFoundError, BlobObject

MID = "68b0667a7e20e7c69b3c7350"
FST_MONTHS = [(2025, m) for m in range(9, 13)] + [(2026, m) for m in range(1, 11)]
NO_V2 = {(2025, 9), (2025, 10), (2025, 11), (2025, 12), (2026, 1), (2026, 2), (2026, 3)}
SCHEMA_GAP = {(2026, 4), (2026, 5)}
EXPECTED_NEEDS = sorted(NO_V2 | SCHEMA_GAP)


def _path(year: int, month: int) -> str:
    return f"machine_id={MID}/quarter={year}Q{(month - 1) // 3 + 1}/month={month}/partition_version=last/part-0.parquet"


class FstStore:
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.by_path = {_path(y, m): (y, m) for y, m in FST_MONTHS}

    def list_blob_names(self, prefix: str) -> list[str]:
        return [name for name in self.by_path if name.startswith(prefix)]

    def read_parquet_metadata(self, name: str) -> BlobObject:
        self.reads.append(name)
        if name not in self.by_path:
            raise BlobNotFoundError(name)
        return BlobObject(name, self.by_path[name], "https://example.test/" + name, None)

    read_blob = read_parquet_metadata


def _summary(content, features):
    if content in NO_V2:
        return ParquetFeatureSummary(100, ["machine_id"], {})
    columns = ["machine_id", "f1"] if content in SCHEMA_GAP else ["machine_id", "f1", "extra"]
    return ParquetFeatureSummary(100, columns, {"f1": 100})


class Lifecycle:
    def __init__(self, record):
        self.record = record

    def get_many(self, machine_ids):
        return [self.record]


def _mongo_record():
    document = {
        "_id": MID,
        "name": "PA30 Finisher Body - ULRPM",
        "status": "active",
        "tags": ["ulrpm"],
        "firstRecorded": {"timestamp": "2025-09-03T13:50:27"},
        "lastRecorded": {"timestamp": "2026-10-08T04:00:06"},
        "endpoints": [
            {"type": "USB-5801_CTC-UEB332", "installation_date": "2026-06-29T09:56:05.544000"}
            for _ in range(4)
        ],
    }
    return _record(document, "test")


def _bronze_record():
    row = {
        "machine_id": MID,
        "machine_name": "PA30 Finisher Body - ULRPM",
        "company": "Indorama.",
        "status": "active",
        "first_recorded": "2025-09-03T13:50:27.000Z",
        "last_recorded": "2026-10-08T04:00:06.000Z",
        "created_at": "2025-08-28T14:23:54.000Z",
        "tags_json": '["ulrpm"]',
    }
    return _record(_normalize_machines_raw_row(row), "test")


def _scan(monkeypatch, record):
    import backfill_dashboard.scanner as scanner_module
    from backfill_dashboard.config import Settings

    settings = Settings(scan_workers=2)
    object.__setattr__(settings, "target_features", ["f1"])
    object.__setattr__(settings, "test_machine_ids", ())
    store = FstStore()
    inventory = SimpleNamespace(list_machine_ids=lambda: [MID])
    scanner = BackfillScanner(settings, inventory, store, SimpleNamespace(row_counts=lambda *_: {}), Lifecycle(record), "healthy")

    def partitions(starts):
        start = min(starts.values())
        out, (y, m) = [], start
        while (y, m) <= (2026, 10):
            out.append(MonthPartition(orchestrator_month_index(y, m), y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out

    monkeypatch.setattr(scanner, "_coverage_partitions", partitions)
    monkeypatch.setattr(scanner_module, "load_fst_schema_contract", lambda _: SimpleNamespace(columns=("machine_id", "f1", "extra"), schema_version="test"))
    monkeypatch.setattr(scanner_module, "inspect_feature_partition", _summary)
    return scanner.scan("parity"), store


def test_records_carry_first_recorded_from_both_sources():
    assert _mongo_record().first_recorded_at == "2025-09-03T13:50:27"
    assert _bronze_record().first_recorded_at == "2025-09-03T13:50:27.000Z"
    assert _mongo_record().installation_at.startswith("2026-06-29")
    assert _bronze_record().installation_at.startswith("2025-08-28")


@pytest.mark.parametrize("make_record", [_mongo_record, _bronze_record], ids=["mongo", "databricks"])
def test_68b0667a_window_and_gaps(monkeypatch, make_record):
    snapshot, store = _scan(monkeypatch, make_record())
    machine = snapshot.machines[0]
    by_month = {(mo.partition.year, mo.partition.month): mo for mo in machine.months}

    needs = sorted(key for key, mo in by_month.items() if mo.status == "needs_backfill")
    assert needs == EXPECTED_NEEDS
    assert len(needs) == 9
    assert machine.status == "needs_backfill"
    assert machine.months_expected == 14
    assert machine.months_complete == 5
    assert machine.pre_install_months == 0
    # Every real partition (2025-09 .. 2026-10) was read under both sources.
    assert {_path(y, m) for y, m in FST_MONTHS} <= set(store.reads)
    # 2025-08 has no data: never an actionable gap (absent or no_source_data).
    if (2025, 8) in by_month:
        assert by_month[(2025, 8)].status == "no_source_data"


def test_sources_produce_identical_month_statuses(monkeypatch):
    def statuses(record):
        snapshot, _ = _scan(monkeypatch, record)
        return {
            (mo.partition.year, mo.partition.month): mo.status
            for mo in snapshot.machines[0].months
            if (mo.partition.year, mo.partition.month) >= (2025, 9)
        }

    assert statuses(_mongo_record()) == statuses(_bronze_record())


def test_partition_before_installation_date_is_still_read():
    from backfill_dashboard.config import Settings

    store = FstStore()
    settings = Settings()
    object.__setattr__(settings, "target_features", ["f1"])
    scanner = BackfillScanner(settings, None, store, None)
    import backfill_dashboard.scanner as scanner_module

    original = scanner_module.inspect_feature_partition
    scanner_module.inspect_feature_partition = _summary
    try:
        month = scanner._scan_month(
            MID, MonthPartition(orchestrator_month_index(2025, 10), 2025, 10), {}, BackfillDecisionPolicy(["f1"]),
            (2025, 9), None, (2026, 6), lambda: False,
            data_start=(2025, 9), partition_months=frozenset(FST_MONTHS),
        )
    finally:
        scanner_module.inspect_feature_partition = original
    assert _path(2025, 10) in store.reads
    assert month.activity_status == "online"
    assert month.status == "needs_backfill"


def test_missing_month_before_data_start_is_no_source_even_with_earlier_installation():
    decision = BackfillDecisionPolicy(["f1"]).missing_partition_after(
        MonthPartition(orchestrator_month_index(2025, 8), 2025, 8),
        coverage_start=(2025, 9),
        installation_start=(2025, 8),
    )
    assert decision.status == "no_source_data"


# --- data_sources -----------------------------------------------------------

def test_data_sources_databricks(monkeypatch):
    from backfill_dashboard.config import Settings
    from backfill_dashboard.data_sources import describe_data_sources

    monkeypatch.setenv("LIFECYCLE_SOURCE", "databricks")
    lifecycle = SimpleNamespace(table="dih_prod.bronze_augury_mh_mongodb.machines_raw", warehouse_id="wh-123", profile="mlops-dev")
    inventory = SimpleNamespace(list_machine_ids=lambda: ["a", "b", "c"])
    info = describe_data_sources(Settings(), lifecycle, "healthy", inventory)

    assert info["lifecycle_source"] == "databricks"
    assert info["lifecycle_status"] == "healthy"
    assert info["lifecycle_table"] == "dih_prod.bronze_augury_mh_mongodb.machines_raw"
    assert info["lifecycle_warehouse_id"] == "wh-123"
    assert info["inventory_source"].startswith("file:")
    assert info["inventory_source"].endswith(".txt")
    assert info["machine_count"] == 3


def test_data_sources_mongo_has_no_secrets(monkeypatch):
    from backfill_dashboard.config import Settings
    from backfill_dashboard.data_sources import describe_data_sources

    monkeypatch.setenv("LIFECYCLE_SOURCE", "Mongo")
    monkeypatch.setenv("MONGODB_URL", "mongodb://user:never-print-me@host/db")
    info = describe_data_sources(Settings(), object(), "healthy", SimpleNamespace(list_machine_ids=lambda: ["a"]))

    assert info["lifecycle_source"] == "mongo"
    assert "lifecycle_table" not in info
    assert "never-print-me" not in repr(info)
    assert info["machine_count"] == 1


def test_default_inventory_source_is_the_curated_file(monkeypatch):
    from pathlib import Path
    from backfill_dashboard.data_sources import _inventory_source, _DASHBOARD_ROOT

    assert _inventory_source(_DASHBOARD_ROOT / "data" / "unique_machine_ids.txt") == "file:data/unique_machine_ids.txt"
    assert _inventory_source(Path("/elsewhere/ids.txt")) == "file:/elsewhere/ids.txt"


def test_status_endpoint_exposes_data_sources(monkeypatch):
    from backfill_dashboard import app as app_module

    monkeypatch.setenv("LIFECYCLE_SOURCE", "off")
    monkeypatch.setattr(app_module.repository, "latest", lambda: {"scan_state": {}, "snapshot": {"machines": []}})
    payload = app_module.backfill_status()

    assert payload["data_sources"]["lifecycle_source"] == "off"
    assert payload["data_sources"]["inventory_source"].startswith("file:")
    assert "snapshot" in payload and "scan_state" in payload


def test_startup_logs_data_sources(monkeypatch, caplog):
    import backfill_dashboard.factory as factory

    monkeypatch.setenv("LIFECYCLE_SOURCE", "off")
    monkeypatch.setattr(factory, "build_lifecycle_provider", lambda: (None, "off"))
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    factory.build_components()

    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("data_sources:")]
    assert len(lines) == 1
    assert "lifecycle_source=off" in lines[0]
    assert "inventory_source=file:" in lines[0]
    assert "machine_count=" in lines[0]
