"""Durable scan snapshot: versioned envelope, blob + local fallback, load on start."""
from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

from backfill_dashboard.config import Settings
from backfill_dashboard.repository import ReportRepository
from backfill_dashboard.scan_snapshot_store import BLOB_NAME, FORMAT, VERSION, ScanSnapshotStore, envelope

SNAPSHOT = {"scan_id": "s1", "generated_at": "2026-10-12T07:00:00+00:00", "source_account": "a", "source_container": "c",
            "target_features": [], "summary": {"total_machines": 1}, "warnings": [],
            "machines": [{"machine_id": "machine-a", "months": []}]}


class FakeBlob:
    def __init__(self, data: bytes | None = None, fail: Exception | None = None):
        self.data, self.fail, self.uploads = data, fail, 0

    def upload_blob(self, data, overwrite=False):
        if self.fail:
            raise self.fail
        assert overwrite is True
        self.data, self.uploads = data, self.uploads + 1

    def download_blob(self):
        if self.fail:
            raise self.fail
        if self.data is None:
            raise _NotFound()
        return SimpleNamespace(readall=lambda: self.data)


class _NotFound(Exception):
    status_code = 404


def _store(tmp_path, blob, logs):
    return ScanSnapshotStore(kind="azure_blob", local_path=tmp_path / "snap.json.gz", account_name="acct",
                             container_name="ctr", blob_factory=lambda: blob, log=logs.append)


def test_save_then_load_round_trips_through_the_blob(tmp_path):
    logs, blob = [], FakeBlob()
    store = _store(tmp_path, blob, logs)
    assert store.save(SNAPSHOT) == f"azure_blob:acct/ctr/{BLOB_NAME}"
    payload = json.loads(gzip.decompress(blob.data))
    assert payload["format"] == FORMAT and payload["version"] == VERSION and payload["snapshot"] == SNAPSHOT
    loaded = store.load()
    assert loaded["snapshot"] == SNAPSHOT and loaded["loaded_from"].startswith("azure_blob:")
    assert not (tmp_path / "snap.json.gz").exists()


def test_blob_failure_falls_back_to_a_local_file(tmp_path):
    logs = []
    store = _store(tmp_path, FakeBlob(fail=PermissionError("denied")), logs)
    assert store.save(SNAPSHOT) == f"local:{tmp_path / 'snap.json.gz'}"
    assert store.load()["loaded_from"].startswith("local:")
    assert any("falling back" in line for line in logs)
    assert not any("denied" in line for line in logs)  # exception type only, no details


def test_version_or_shape_mismatch_is_ignored_and_logged(tmp_path):
    logs = []
    bad_version = {**envelope(SNAPSHOT), "version": VERSION + 1}
    store = _store(tmp_path, FakeBlob(gzip.compress(json.dumps(bad_version).encode())), logs)
    assert store.load() is None and any("version" in line for line in logs)
    malformed = {**envelope(SNAPSHOT), "snapshot": {"machines": "nope"}}
    store = _store(tmp_path, FakeBlob(gzip.compress(json.dumps(malformed).encode())), logs)
    assert store.load() is None and any("malformed" in line for line in logs)
    store = _store(tmp_path, FakeBlob(b"not gzip"), logs)
    assert store.load() is None and any("unreadable" in line for line in logs)
    store = _store(tmp_path, FakeBlob(None), logs)
    assert store.load() is None


def test_store_kind_defaults(tmp_path):
    state = tmp_path / "state.json"
    assert ScanSnapshotStore.from_env(state, {}).kind == "local"
    assert ScanSnapshotStore.from_env(state, {"DATABRICKS_APP_PORT": "8000"}).kind == "azure_blob"
    assert ScanSnapshotStore.from_env(state, {"DATABRICKS_APP_PORT": "8000", "BACKFILL_CONTROL_STORE": "local_json"}).kind == "local"
    assert ScanSnapshotStore.from_env(state, {"SCAN_SNAPSHOT_STORE": "off"}).kind == "off"
    store = ScanSnapshotStore.from_env(state, {"DATABRICKS_APP_PORT": "8000"})
    assert store.describe() == f"azure_blob:aifleetmlopsfstbackfill/fst-backfill/{BLOB_NAME}"


def test_repository_restores_the_saved_scan_on_start_and_saves_after_a_scan(tmp_path):
    logs, blob = [], FakeBlob()
    store = _store(tmp_path, blob, logs)
    store.save(SNAPSHOT)
    settings = Settings(state_path=tmp_path / "missing_state.json")
    repo = ReportRepository(settings, snapshot_store=store, load_in_background=False)
    latest = repo.latest()
    assert latest["snapshot"]["scan_id"] == "s1"
    assert latest["persistence"]["loaded_from"].startswith("azure_blob:") and latest["persistence"]["loading"] is False

    class Scanner:
        def scan(self, **kwargs):
            from backfill_dashboard.models import DashboardSnapshot
            snap = DashboardSnapshot.empty(account="a", container="c", target_features=[])
            snap.scan_id, snap.generated_at = "s2", "2026-10-12T08:00:00+00:00"
            return snap

    state, started = repo.start_scan(source_account="a", source_container="c")
    assert started
    repo.run_scan(Scanner(), state.scan_id)
    assert blob.uploads == 2 and json.loads(gzip.decompress(blob.data))["snapshot"]["scan_id"] == "s2"
    assert repo.latest()["persistence"]["saved_to"].startswith("azure_blob:")
    # An older durable copy never replaces a newer in-memory scan.
    blob.data = gzip.compress(json.dumps(envelope(SNAPSHOT)).encode())
    assert repo.load_saved_snapshot() is False and repo.latest()["snapshot"]["scan_id"] == "s2"


def test_repository_without_a_store_keeps_the_legacy_behaviour(tmp_path):
    repo = ReportRepository(Settings(state_path=tmp_path / "s.json"))
    assert repo.latest()["persistence"] == {"store": "off", "saved_at": None, "loaded_from": None, "loading": False}
