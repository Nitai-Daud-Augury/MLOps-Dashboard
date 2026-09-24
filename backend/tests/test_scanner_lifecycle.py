from __future__ import annotations

from types import SimpleNamespace

import pytest

from backfill_dashboard.inventory_models import MachineRecord
from backfill_dashboard.models import MonthPartition
from backfill_dashboard.parquet_inspector import ParquetFeatureSummary
from backfill_dashboard.policy import BackfillDecisionPolicy
from backfill_dashboard.scanner import BackfillScanner
from backfill_dashboard.storage import BlobNotFoundError, BlobObject


class BlobStore:
    def __init__(self, populated: set[str] = set()):
        self.populated = populated
        self.reads: list[str] = []

    def list_blob_names(self, prefix: str) -> list[str]:
        return []

    def read_parquet_metadata(self, name: str) -> BlobObject:
        self.reads.append(name)
        if name not in self.populated:
            raise BlobNotFoundError(name)
        return BlobObject(name, b"parquet", "https://example.test/" + name, None)

    def read_blob(self, name: str) -> BlobObject:
        return self.read_parquet_metadata(name)


def _scanner(blob_store: BlobStore) -> BackfillScanner:
    from backfill_dashboard.config import Settings

    settings = Settings()
    object.__setattr__(settings, "target_features", ["f1"])
    return BackfillScanner(settings, None, blob_store, None)


def _missing(
    scanner: BackfillScanner,
    month: int,
    cutoff: tuple[int, int] | None,
    installation: tuple[int, int] | None = None,
):
    partition = MonthPartition(month, 2026, month)
    policy = BackfillDecisionPolicy(["f1"])
    return scanner._scan_month(
        "m1", partition, {}, policy, (2026, 6), cutoff, installation, lambda: False
    )


def test_month_before_augury_installation_is_not_a_backfill_gap():
    scanner = _scanner(BlobStore())

    month = _missing(scanner, 5, None, (2026, 8))

    assert month.status == "no_source_data"
    assert month.activity_status == "not_installed"
    assert "installation" in month.reason


def test_installed_month_without_fst_data_is_offline_and_actionable():
    scanner = _scanner(BlobStore())

    month = _missing(scanner, 8, None, (2026, 8))

    assert month.status == "needs_backfill"
    assert month.activity_status == "offline"


def test_deactivated_cutoff_month_is_actionable_but_later_missing_month_is_not():
    scanner = _scanner(BlobStore())
    assert _missing(scanner, 6, (2026, 6)).status == "needs_backfill"
    assert _missing(scanner, 7, (2026, 6)).status == "no_source_data"


def test_active_stale_last_recorded_has_no_cutoff():
    scanner = _scanner(BlobStore())
    assert _missing(scanner, 7, None).status == "needs_backfill"


def test_populated_partition_after_cutoff_is_still_inspected(monkeypatch: pytest.MonkeyPatch):
    blob_store = BlobStore({"machine_id=m1/quarter=2026Q3/month=7/partition_version=last/part-0.parquet"})
    scanner = _scanner(blob_store)
    monkeypatch.setattr(
        "backfill_dashboard.scanner.inspect_feature_partition",
        lambda content, features: ParquetFeatureSummary(2, ["f1"], {"f1": 2}),
    )
    month = _missing(scanner, 7, (2026, 6))
    assert month.status == "backfilled"
    assert month.activity_status == "online"
    assert blob_store.reads


class Inventory:
    def __init__(self, record: MachineRecord | None = None, error: Exception | None = None):
        self.record, self.error = record, error

    def list_machine_ids(self) -> list[str]:
        return ["m1"]

    def get_many(self, machine_ids: list[str]) -> list[MachineRecord]:
        if self.error:
            raise self.error
        return [self.record] if self.record else []


def _full_scan(monkeypatch: pytest.MonkeyPatch, lifecycle, blob_store: BlobStore):
    from backfill_dashboard.config import Settings
    import backfill_dashboard.scanner as scanner_module

    settings = Settings(scan_workers=1)
    object.__setattr__(settings, "target_features", ["f1"])
    scanner = BackfillScanner(settings, Inventory(), blob_store, SimpleNamespace(row_counts=lambda *_: {}), lifecycle, "healthy")
    monkeypatch.setattr(scanner, "_discover_coverage_starts", lambda *args, **kwargs: {"m1": (2026, 6)})
    monkeypatch.setattr(scanner, "_coverage_partitions", lambda starts: [MonthPartition(6, 2026, 6), MonthPartition(7, 2026, 7)])
    monkeypatch.setattr(scanner_module, "load_fst_schema_contract", lambda _: SimpleNamespace(columns=("f1",), schema_version="test"))
    monkeypatch.setattr(scanner_module, "inspect_feature_partition", lambda content, features: ParquetFeatureSummary(2, ["f1"], {"f1": 2}))
    return scanner.scan("scan-1")


def test_lifecycle_lookup_failure_is_fail_open_and_warns(monkeypatch: pytest.MonkeyPatch):
    snapshot = _full_scan(
        monkeypatch,
        Inventory(error=RuntimeError("Mongo unavailable")),
        BlobStore(),
    )
    assert any("lifecycle lookup failed" in warning.lower() for warning in snapshot.warnings)
    assert snapshot.machines[0].months[1].status == "needs_backfill"


def test_deactivated_full_scan_rolls_up_populated_cutoff_month(monkeypatch: pytest.MonkeyPatch):
    record = MachineRecord("m1", status="deactivated", last_recorded_at="2026-06-15T00:00:00Z")
    snapshot = _full_scan(monkeypatch, Inventory(record=record), BlobStore({
        "machine_id=m1/quarter=2026Q2/month=6/partition_version=last/part-0.parquet",
    }))
    assert snapshot.machines[0].status == "backfilled"
    assert [month.status for month in snapshot.machines[0].months] == ["backfilled", "no_source_data"]
    assert "1 cutoff" in snapshot.warnings[0]


def test_installation_date_overrides_an_impossibly_early_fst_partition(monkeypatch: pytest.MonkeyPatch):
    record = MachineRecord(
        "m1",
        status="active",
        installation_at="2026-08-14T12:00:00Z",
    )
    snapshot = _full_scan(
        monkeypatch,
        Inventory(record=record),
        BlobStore({
            "machine_id=m1/quarter=2026Q2/month=6/partition_version=last/part-0.parquet",
        }),
    )

    machine = snapshot.machines[0]
    assert machine.installation_month == "2026-08"
    assert machine.pre_install_months == 2
    assert machine.months_expected == 0
    assert [month.activity_status for month in machine.months] == [
        "not_installed",
        "not_installed",
    ]


def test_scan_carries_inventory_identity_to_machine_status(monkeypatch: pytest.MonkeyPatch):
    record = MachineRecord("m1", display_name="ULRPM E2E test #5", is_test_machine=True)
    snapshot = _full_scan(monkeypatch, Inventory(record=record), BlobStore())

    assert snapshot.machines[0].display_name == "ULRPM E2E test #5"
    assert snapshot.machines[0].is_test_machine
