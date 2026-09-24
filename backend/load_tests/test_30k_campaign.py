"""Explicit scale gate: run separately from the fast unit suite."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from backfill_dashboard.control_plane import ControlPlane
from backfill_dashboard.inventory_models import MachinePage, MachineSearchQuery
from backfill_dashboard.inventory_provider import _record, decode_cursor, encode_cursor


class ThirtyThousandMachines:
    size = 30_000

    def search(self, query: MachineSearchQuery) -> MachinePage:
        start = int(decode_cursor(query.cursor) or 0)
        end = min(self.size, start + query.limit)
        records = [_record({"_id": f"machine-{index:05d}", "tags": [], "endpoints": [{"type": "apus_alpha"}], "status": "active"}, "load-v1") for index in range(start, end)]
        return MachinePage(records, encode_cursor(str(end)) if end < self.size else None, "load-v1", self.size)


def test_plan_30k_machines_and_780k_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKFILL_ESTIMATE_SIGNING_KEY", "load-test-only")
    monkeypatch.setenv("BACKFILL_MAX_WORK_ITEMS", "800000")
    settings = SimpleNamespace(control_plane_db_path=tmp_path / "load.sqlite3", estimate_ttl_seconds=900,
                               dispatch_enabled=False, production_mode=False)
    plane = ControlPlane(settings, ThirtyThousandMachines(), "healthy", None)
    started = time.monotonic()
    assert plane.synchronizer.sync()["machine_count"] == 30_000
    estimate = plane.estimates.create({"selection": {"inventory_version": plane.inventory.get_version(), "filter": {"eligible": True}},
        "start_at": "2024-01-01", "end_at": "2026-02-01", "feature_set_version": "load-v1",
        "standard_window_days": 30, "ulrpm_window_days": 5})
    campaign = plane.campaign_service.submit(estimate["estimate_id"], estimate["estimate_signature"],
        name="30k load gate", created_by="test", production=False, confirmation_text="", ulrpm_confirmation_text="")
    assert campaign["work_item_counts"] == {"blocked": 750_000, "ready": 30_000}
    assert time.monotonic() - started < 60
