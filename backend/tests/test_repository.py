from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from backfill_dashboard.config import Settings
from backfill_dashboard.models import DashboardSnapshot
from backfill_dashboard.repository import ReportRepository


def test_latest_normalizes_cached_augury_machine_urls(tmp_path):
    machine_id = "683ec59079fecb5a5a240478"
    state_path = tmp_path / "dashboard_state.json"
    state_path.write_text(
        json.dumps(
            {
                "scan_id": "cached",
                "machines": [
                    {
                        "machine_id": machine_id,
                        "augury_url": f"https://app.augury.com/machines/{machine_id}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(state_path=state_path)

    snapshot = ReportRepository(settings).latest()["snapshot"]

    assert snapshot["machines"][0]["augury_url"] == (
        "https://app.augury.com/#/machine_health/machines/"
        "683ec59079fecb5a5a240478"
    )


class _FakeScanner:
    def __init__(self, snapshot: DashboardSnapshot) -> None:
        self.snapshot = snapshot

    def scan(self, scan_id: str, progress_callback, machine_progress_callback,
             machine_result_callback, is_cancelled):
        progress_callback(1, 2, "inspecting Parquet partitions")
        machine_progress_callback(1, 1)
        return self.snapshot


def test_start_scan_clears_a_stale_snapshot_and_reports_requested_source(tmp_path):
    state_path = tmp_path / "dashboard_state.json"
    state_path.write_text(
        json.dumps(asdict(DashboardSnapshot.empty(
            account="test-account",
            container="test-container",
            target_features=["v2"],
        ))),
        encoding="utf-8",
    )
    settings = Settings(state_path=state_path)
    repository = ReportRepository(settings)

    state, is_new = repository.start_scan(
        source_account="prod-account",
        source_container="prod-container",
    )

    assert is_new is True
    assert state.running is True
    latest = repository.latest()
    assert latest["snapshot"]["source_account"] == "prod-account"
    assert latest["snapshot"]["source_container"] == "prod-container"
    assert latest["snapshot"]["scan_id"] == "not-scanned"
    assert latest["scan_state"]["phase"] == "queued"


def test_reset_prevents_an_old_scan_from_overwriting_a_newer_scan(tmp_path):
    settings = Settings(state_path=tmp_path / "dashboard_state.json")
    repository = ReportRepository(settings)
    state, _ = repository.start_scan(source_account="prod", source_container="container")
    old_scan_id = state.scan_id
    repository.reset_scan()

    snapshot = DashboardSnapshot.empty(account="old", container="old", target_features=[])
    repository.run_scan(_FakeScanner(snapshot), old_scan_id)

    latest = repository.latest()
    assert latest["snapshot"]["source_account"] == settings.fst_account
    assert latest["snapshot"]["scan_id"] == "not-scanned"


def test_scan_older_than_timeout_stays_running_while_progress_is_recent(tmp_path):
    settings = Settings(
        state_path=tmp_path / "dashboard_state.json",
        scan_idle_timeout_seconds=60,
    )
    repository = ReportRepository(settings)
    state, _ = repository.start_scan(source_account="prod", source_container="container")
    state.started_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    state.last_progress_at = datetime.now(timezone.utc).isoformat()

    latest = repository.latest()

    assert latest["scan_state"]["running"] is True
    assert latest["scan_state"]["phase"] == "queued"


def test_scan_expires_only_after_progress_is_idle(tmp_path):
    settings = Settings(
        state_path=tmp_path / "dashboard_state.json",
        scan_idle_timeout_seconds=60,
    )
    repository = ReportRepository(settings)
    state, _ = repository.start_scan(source_account="prod", source_container="container")
    stale = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    state.started_at = stale
    state.last_progress_at = stale
    state.last_heartbeat_at = stale
    cancellation = repository._cancellations[state.scan_id]

    latest = repository.latest()

    assert latest["scan_state"]["running"] is False
    assert latest["scan_state"]["phase"] == "timed out"
    assert "2m 00s of inactivity" in latest["scan_state"]["error"]
    assert cancellation.is_set()


def test_progress_heartbeat_updates_only_the_active_scan(tmp_path):
    settings = Settings(state_path=tmp_path / "dashboard_state.json")
    repository = ReportRepository(settings)
    state, _ = repository.start_scan(source_account="prod", source_container="container")
    old_heartbeat = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    state.last_progress_at = old_heartbeat

    repository.update_scan_progress("obsolete-scan", 1, 10, "ignored")
    assert state.last_progress_at == old_heartbeat

    repository.update_scan_progress(state.scan_id, 1, 10, "reading blobs")
    assert state.last_progress_at != old_heartbeat
    assert state.completed_partitions == 1
    assert state.phase == "reading blobs"


def test_scan_timeout_falls_back_to_started_at_without_heartbeat(tmp_path):
    settings = Settings(
        state_path=tmp_path / "dashboard_state.json",
        scan_idle_timeout_seconds=60,
    )
    repository = ReportRepository(settings)
    state, _ = repository.start_scan(source_account="prod", source_container="container")
    state.started_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    state.last_progress_at = None
    state.last_heartbeat_at = None

    latest = repository.latest()

    assert latest["scan_state"]["phase"] == "timed out"


class _BlockingScanner:
    def __init__(self) -> None:
        self.release = threading.Event()

    def scan(self, scan_id: str, progress_callback, machine_progress_callback,
             machine_result_callback, is_cancelled):
        self.release.wait(timeout=5)
        return DashboardSnapshot.empty(account="prod", container="container", target_features=[])


def test_active_scan_heartbeats_during_one_long_partition(tmp_path):
    settings = Settings(
        state_path=tmp_path / "dashboard_state.json",
        scan_idle_timeout_seconds=1,
        scan_heartbeat_interval_seconds=1,
    )
    repository = ReportRepository(settings)
    state, _ = repository.start_scan(source_account="prod", source_container="container")
    stale = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    state.last_progress_at = stale
    state.last_heartbeat_at = stale
    scanner = _BlockingScanner()
    worker = threading.Thread(target=repository.run_scan, args=(scanner, state.scan_id))
    worker.start()

    deadline = time.monotonic() + 3
    while state.last_heartbeat_at == stale and time.monotonic() < deadline:
        time.sleep(0.05)

    assert state.last_heartbeat_at != stale
    assert repository.latest()["scan_state"]["running"] is True

    scanner.release.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
