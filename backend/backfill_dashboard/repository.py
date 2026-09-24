from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .activity import normalize_snapshot_coverage
from .config import Settings
from .models import DashboardSnapshot, ScanState
from .scanner import BackfillScanner, ScanCancelled

DEFAULT_SCAN_IDLE_TIMEOUT_SECONDS = 10 * 60


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if not minutes:
        return f"{remaining_seconds}s"
    return f"{minutes}m {remaining_seconds:02d}s"


class ReportRepository:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._scan_state = ScanState(running=False)
        self._latest: dict | None = self._load_latest()
        self._cancellations: dict[str, threading.Event] = {}

    @property
    def scan_state(self) -> ScanState:
        with self._lock:
            return self._scan_state

    def latest(self) -> dict:
        self._auto_expire_stuck_scan()
        with self._lock:
            if self._latest:
                self._latest = self._normalize_machine_urls(self._latest)
                return {
                    "scan_state": asdict(self._scan_state),
                    "snapshot": self._latest,
                }
        snapshot = DashboardSnapshot.empty(
            account=self._scan_state.source_account or self.settings.fst_account,
            container=self._scan_state.source_container or self.settings.fst_container,
            target_features=self.settings.target_features,
        )
        return {
            "scan_state": asdict(self._scan_state),
            "snapshot": asdict(snapshot),
        }

    def reset_scan(self) -> ScanState:
        """Force-clear a stuck scan so a new one can start."""
        with self._lock:
            for cancellation in self._cancellations.values():
                cancellation.set()
            self._scan_state = ScanState(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                error="Scan was manually reset.",
                phase="reset",
            )
            self._latest = None
            return self._scan_state

    def start_scan(self, *, source_account: str, source_container: str) -> tuple[ScanState, bool]:
        """Return (state, is_new). If already running, is_new=False."""
        with self._lock:
            if self._scan_state.running:
                return self._scan_state, False
            now = datetime.now(timezone.utc).isoformat()
            self._scan_state = ScanState(
                running=True,
                started_at=now,
                last_progress_at=now,
                last_heartbeat_at=now,
                source_account=source_account,
                source_container=source_container,
                phase="queued",
            )
            # Never present a previous source as if it were the currently selected one.
            self._latest = asdict(DashboardSnapshot.empty(account=source_account, container=source_container, target_features=self.settings.target_features))
            self._latest["warnings"] = ["Scan in progress. Completed machines appear as they finish."]
            self._cancellations[self._scan_state.scan_id] = threading.Event()
            return self._scan_state, True

    def run_scan(self, scanner: BackfillScanner, scan_id: str) -> None:
        with self._lock:
            cancellation = self._cancellations.get(scan_id)
        if cancellation is None:
            return
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_scan,
            args=(scan_id, heartbeat_stop),
            name=f"backfill-scan-heartbeat-{scan_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            snapshot = scanner.scan(
                scan_id=scan_id,
                progress_callback=lambda completed, total, phase: self.update_scan_progress(
                    scan_id, completed, total, phase
                ),
                machine_progress_callback=lambda completed, total: self.update_machine_progress(
                    scan_id, completed, total
                ),
                machine_result_callback=lambda machine: self.add_partial_machine(scan_id, asdict(machine)),
                is_cancelled=lambda: cancellation.is_set() or not self._is_active_scan(scan_id),
            )
            payload = asdict(snapshot)
            with self._lock:
                if not self._is_active_scan(scan_id):
                    return
                self._latest = payload
                self._scan_state.running = False
                self._scan_state.finished_at = datetime.now(timezone.utc).isoformat()
                self._scan_state.error = None
                self._scan_state.phase = "complete"
                self._persist_latest(payload)
                self._cancellations.pop(scan_id, None)
        except ScanCancelled:
            with self._lock:
                self._cancellations.pop(scan_id, None)
            return
        except Exception as exc:
            with self._lock:
                if not self._is_active_scan(scan_id):
                    return
                self._scan_state.running = False
                self._scan_state.finished_at = datetime.now(timezone.utc).isoformat()
                self._scan_state.error = str(exc)
                self._scan_state.phase = "failed"
                self._cancellations.pop(scan_id, None)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)

    def update_scan_progress(self, scan_id: str, completed: int, total: int, phase: str) -> None:
        with self._lock:
            if not self._is_active_scan(scan_id):
                return
            self._scan_state.completed_partitions = completed
            self._scan_state.total_partitions = total
            self._scan_state.phase = phase
            self._scan_state.last_progress_at = datetime.now(timezone.utc).isoformat()
            self._scan_state.last_heartbeat_at = self._scan_state.last_progress_at

    def update_machine_progress(self, scan_id: str, completed: int, total: int) -> None:
        with self._lock:
            if not self._is_active_scan(scan_id):
                return
            self._scan_state.completed_machines = completed
            self._scan_state.total_machines = total
            self._scan_state.last_progress_at = datetime.now(timezone.utc).isoformat()
            self._scan_state.last_heartbeat_at = self._scan_state.last_progress_at

    def add_partial_machine(self, scan_id: str, machine: dict) -> None:
        with self._lock:
            if not self._is_active_scan(scan_id):
                return
            self._scan_state.last_progress_at = datetime.now(timezone.utc).isoformat()
            self._scan_state.last_heartbeat_at = self._scan_state.last_progress_at
            if not self._latest:
                return
            machines = self._latest.setdefault("machines", [])
            machines.append(machine)

    def _is_active_scan(self, scan_id: str) -> bool:
        return self._scan_state.running and self._scan_state.scan_id == scan_id

    def _heartbeat_scan(self, scan_id: str, stop: threading.Event) -> None:
        interval = max(1, int(self.settings.scan_heartbeat_interval_seconds))
        while not stop.wait(interval):
            with self._lock:
                if not self._is_active_scan(scan_id):
                    return
                self._scan_state.last_heartbeat_at = datetime.now(timezone.utc).isoformat()

    def _load_latest(self) -> dict | None:
        if not self.settings.state_path.exists():
            return None
        try:
            return self._normalize_machine_urls(
                json.loads(self.settings.state_path.read_text(encoding="utf-8"))
            )
        except Exception:
            return None

    def _persist_latest(self, snapshot: dict) -> None:
        path: Path = self.settings.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._normalize_machine_urls(snapshot), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _normalize_machine_urls(self, snapshot: dict) -> dict:
        snapshot = normalize_snapshot_coverage(snapshot)
        for machine in snapshot.get("machines", []):
            machine_id = machine.get("machine_id")
            if machine_id:
                machine["augury_url"] = self.settings.augury_machine_url_template.format(
                    machine_id=machine_id
                )
        return snapshot

    def _auto_expire_stuck_scan(self) -> None:
        with self._lock:
            if not self._scan_state.running or not self._scan_state.started_at:
                return
            activity_at = (
                self._scan_state.last_heartbeat_at
                or self._scan_state.last_progress_at
                or self._scan_state.started_at
            )
            if not activity_at:
                return
            last_activity = datetime.fromisoformat(activity_at)
            idle_seconds = (datetime.now(timezone.utc) - last_activity).total_seconds()
            timeout_seconds = getattr(
                self.settings, "scan_idle_timeout_seconds", DEFAULT_SCAN_IDLE_TIMEOUT_SECONDS
            )
            if idle_seconds > timeout_seconds:
                cancellation = self._cancellations.get(self._scan_state.scan_id)
                if cancellation:
                    cancellation.set()
                self._scan_state.running = False
                self._scan_state.finished_at = datetime.now(timezone.utc).isoformat()
                self._scan_state.error = (
                    f"Scan timed out after {_format_elapsed(idle_seconds)} of inactivity. The background "
                    "task may have stopped reporting progress. Try again."
                )
                self._scan_state.phase = "timed out"
