from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backfill_dashboard.config import Settings, load_test_machine_ids
from backfill_dashboard.inventory import FileMachineInventoryProvider
from backfill_dashboard.inventory_provider import FileMachineInventoryAdapter
from backfill_dashboard.repository import ReportRepository
from backfill_dashboard.storage import BlobNotFoundError


TEST_MACHINE_ID = "6a747b591f957aebfe6c59cf"


def test_default_registry_includes_known_test_machine():
    settings = Settings()
    assert TEST_MACHINE_ID in settings.test_machine_ids
    assert isinstance(settings.test_machine_ids, frozenset)


def test_registry_loader_validates_deduplicates_and_accepts_additive_ids(tmp_path):
    registry = tmp_path / "test_ids.txt"
    registry.write_text(f"# test IDs\n{TEST_MACHINE_ID}\n{TEST_MACHINE_ID.upper()} # duplicate\n", encoding="utf-8")
    assert load_test_machine_ids(registry, TEST_MACHINE_ID) == frozenset({TEST_MACHINE_ID})

    registry.write_text("not-an-object-id\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid test machine ID"):
        load_test_machine_ids(registry)


def test_missing_override_registry_fails_clearly(tmp_path):
    with pytest.raises(FileNotFoundError, match="Test machine registry not found"):
        Settings(test_machine_ids_file=tmp_path / "missing.txt")


def test_environment_registry_override_resolves_relative_to_project_root(monkeypatch, tmp_path):
    import backfill_dashboard.config as config

    registry = tmp_path / "custom" / "test_ids.txt"
    registry.parent.mkdir()
    registry.write_text(f"{TEST_MACHINE_ID}\n", encoding="utf-8")
    monkeypatch.setattr(config, "_dashboard_root", tmp_path)
    monkeypatch.setenv("MLOPS_TEST_MACHINE_IDS_FILE", "custom/test_ids.txt")
    monkeypatch.setenv("MLOPS_TEST_MACHINE_IDS", TEST_MACHINE_ID)
    assert Settings().test_machine_ids_file == registry
    assert Settings().test_machine_ids == frozenset({TEST_MACHINE_ID})


def test_file_inventory_adapter_marks_explicit_test_id_without_mongo(tmp_path):
    inventory_file = tmp_path / "machines.txt"
    inventory_file.write_text(f"{TEST_MACHINE_ID}\n", encoding="utf-8")
    adapter = FileMachineInventoryAdapter(
        FileMachineInventoryProvider(inventory_file),
        {TEST_MACHINE_ID},
    )

    record = adapter.get_many([TEST_MACHINE_ID])[0]
    assert record.is_test_machine is True
    assert record.display_name == TEST_MACHINE_ID


def test_scanner_falls_back_to_registry_when_lifecycle_is_unavailable(monkeypatch):
    import backfill_dashboard.scanner as scanner_module
    from backfill_dashboard.models import MonthPartition
    from backfill_dashboard.parquet_inspector import ParquetFeatureSummary
    from backfill_dashboard.scanner import BackfillScanner

    settings = Settings(scan_workers=1, test_machine_ids=frozenset({TEST_MACHINE_ID}))
    object.__setattr__(settings, "target_features", ["f1"])
    inventory = SimpleNamespace(
        list_machine_ids=lambda: [TEST_MACHINE_ID],
        get_many=lambda _: (_ for _ in ()).throw(RuntimeError("lifecycle unavailable")),
    )
    blob_store = SimpleNamespace(
        list_blob_names=lambda prefix: [],
        read_parquet_metadata=lambda name: (_ for _ in ()).throw(BlobNotFoundError(name)),
    )
    scanner = BackfillScanner(settings, inventory, blob_store, SimpleNamespace(row_counts=lambda *_: {}))
    monkeypatch.setattr(scanner, "_discover_coverage_starts", lambda *args, **kwargs: {TEST_MACHINE_ID: (2026, 6)})
    monkeypatch.setattr(scanner, "_coverage_partitions", lambda starts: [MonthPartition(6, 2026, 6)])
    monkeypatch.setattr(scanner_module, "load_fst_schema_contract", lambda _: SimpleNamespace(columns=("f1",), schema_version="test"))
    monkeypatch.setattr(scanner_module, "inspect_feature_partition", lambda *_: ParquetFeatureSummary(1, ["f1"], {"f1": 1}))

    machine = scanner.scan("scan-test").machines[0]
    assert machine.is_test_machine is True
    assert asdict(machine)["is_test_machine"] is True


def test_cached_legacy_snapshot_is_enriched_without_rewriting_state(tmp_path, monkeypatch):
    from backfill_dashboard import app as app_module

    state_file = tmp_path / "snapshot.json"
    legacy = {"scan_id": "old", "machines": [{"machine_id": TEST_MACHINE_ID, "display_name": TEST_MACHINE_ID}]}
    original = json.dumps(legacy)
    state_file.write_text(original, encoding="utf-8")

    repository = ReportRepository(Settings(state_path=state_file))
    payload = repository.latest()
    assert payload["snapshot"]["machines"][0]["is_test_machine"] is True

    # The status API returns the same normalized identity field asdict-compatible payload.
    monkeypatch.setattr(app_module, "repository", repository)
    assert app_module.backfill_status()["snapshot"]["machines"][0]["is_test_machine"] is True

    assert state_file.read_text(encoding="utf-8") == original


def test_existing_true_snapshot_metadata_is_preserved(tmp_path):
    state_file = tmp_path / "snapshot.json"
    state_file.write_text(json.dumps({"machines": [
        {"machine_id": "6847d5ed7fbbf944adb1eb0a", "is_test_machine": True}
    ]}), encoding="utf-8")

    payload = ReportRepository(Settings(state_path=state_file)).latest()

    assert payload["snapshot"]["machines"][0]["is_test_machine"] is True
