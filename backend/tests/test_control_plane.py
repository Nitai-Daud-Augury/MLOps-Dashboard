from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backfill_dashboard.control_plane import ControlPlane
from backfill_dashboard.inventory_models import MachinePage, MachineSearchQuery
from backfill_dashboard.inventory_provider import _record, decode_cursor, encode_cursor
from backfill_dashboard.control_plane.dispatcher import Dispatcher
from backfill_dashboard.control_plane.safety_policy import evaluate_campaign
from backfill_dashboard.control_plane.machine_control import apply_machine_action
from backfill_dashboard.control_plane.workflow_adapter import WorkflowIdentity
from backfill_dashboard.control_plane.database import Database
from backfill_dashboard.control_plane.schema import SCHEMA


class Source:
    def __init__(self):
        self.records = [
            _record({"_id": "machine-standard", "tags": [], "endpoints": [{"type": "apus_alpha"}], "status": "active"}, "v1"),
            _record({"_id": "machine-ulrpm", "tags": [], "endpoints": [{"type": "IEPEDAQ.rev_a_HS-179"}], "status": "active"}, "v1"),
            _record({"_id": "machine-unknown", "tags": [], "endpoints": [{"type": "future"}], "status": "active"}, "v1"),
        ]

    def search(self, query: MachineSearchQuery):
        start = int(decode_cursor(query.cursor) or 0)
        page = self.records[start:start + query.limit]
        next_cursor = encode_cursor(str(start + len(page))) if start + len(page) < len(self.records) else None
        return MachinePage(page, next_cursor, "v1", len(self.records))


class FlakySource(Source):
    def __init__(self):
        super().__init__()
        self.failures = 1

    def search(self, query: MachineSearchQuery):
        if self.failures:
            self.failures -= 1
            raise TimeoutError("temporary dependency timeout")
        return super().search(query)


def build_plane(tmp_path: Path, monkeypatch) -> ControlPlane:
    monkeypatch.setenv("BACKFILL_ESTIMATE_SIGNING_KEY", "test-secret")
    monkeypatch.setenv("BACKFILL_AZURE_REGION", "eastus")
    monkeypatch.setenv("BACKFILL_STANDARD_VM_SKU", "Standard_D4s_v5")
    monkeypatch.setenv("BACKFILL_STANDARD_NODE_MEMORY_MIB", "16384")
    monkeypatch.setenv("BACKFILL_STANDARD_NODE_CPU_MILLICORES", "4000")
    monkeypatch.setenv("BACKFILL_STANDARD_NODES_JSON", '[{"name":"s1","available_memory_mib":16384,"available_cpu_millicores":4000,"available_pod_slots":10}]')
    monkeypatch.setenv("BACKFILL_ULRPM_NODES_JSON", '[{"name":"u1","available_memory_mib":128000,"available_cpu_millicores":8000,"available_pod_slots":10}]')
    monkeypatch.setenv("BACKFILL_STANDARD_CAPACITY_VERIFIED", "1")
    monkeypatch.setenv("BACKFILL_ULRPM_CAPACITY_VERIFIED", "1")
    monkeypatch.setenv("BACKFILL_PRICE_SPOT_STANDARD_D4S_V5", "0.2")
    monkeypatch.setenv("BACKFILL_PRICE_ONDEMAND_STANDARD_E64DS_V5", "4.0")
    settings = SimpleNamespace(control_plane_db_path=tmp_path / "control.sqlite3", estimate_ttl_seconds=900,
                               dispatch_enabled=False, production_mode=False)
    return ControlPlane(settings, Source(), "healthy", manifest_writer=None)


def test_inventory_snapshot_estimate_and_campaign_are_durable(tmp_path, monkeypatch):
    plane = build_plane(tmp_path, monkeypatch)
    result = plane.synchronizer.sync()
    assert result["machine_count"] == 3
    assert plane.inventory.search(MachineSearchQuery(limit=2)).next_cursor
    assert plane.inventory.get_facets(MachineSearchQuery()).cohorts == {"standard": 1, "ulrpm": 1, "unknown": 1}
    estimate = plane.estimates.create({
        "selection": {"inventory_version": plane.inventory.get_version(), "filter": {"eligible": None}, "excluded_machine_ids": [], "explicit_machine_ids": []},
        "start_at": "2026-01-01", "end_at": "2026-01-11", "feature_set_version": "features-v1",
        "standard_window_days": 5, "ulrpm_window_days": 5,
    })
    assert estimate["lanes"]["standard"]["machine_count"] == 1
    assert estimate["lanes"]["ulrpm"]["machine_count"] == 1
    assert estimate["excluded_unknown_or_ineligible"] == 1
    campaign = plane.campaign_service.submit(estimate["estimate_id"], estimate["estimate_signature"],
        name="safe campaign", created_by="tester", production=False, confirmation_text="", ulrpm_confirmation_text="")
    assert campaign["work_item_counts"] == {"blocked": 2, "ready": 2}
    reopened = build_plane(tmp_path, monkeypatch)
    assert reopened.campaigns.get(campaign["id"])["work_item_counts"] == campaign["work_item_counts"]

    leased = reopened.campaigns.lease("standard", 10, "worker")
    assert len(leased) == 1
    reopened.campaigns.complete(leased[0]["idempotency_key"], "succeeded")
    state = reopened.campaigns.get(campaign["id"])
    assert state["work_item_counts"]["ready"] == 2
    assert state["work_item_counts"]["succeeded"] == 1


def test_signed_estimate_rejects_changed_inventory(tmp_path, monkeypatch):
    plane = build_plane(tmp_path, monkeypatch)
    plane.synchronizer.sync()
    estimate = plane.estimates.create({"selection": {"inventory_version": plane.inventory.get_version(), "filter": {"eligible": True}},
        "start_at": "2026-01-01", "end_at": "2026-01-02", "feature_set_version": "v1",
        "standard_window_days": 1, "ulrpm_window_days": 1})
    plane.database.set_metadata("inventory_version", "v2")
    try:
        plane.campaign_service.submit(estimate["estimate_id"], estimate["estimate_signature"], name="", created_by="x",
                                      production=False, confirmation_text="", ulrpm_confirmation_text="")
    except ValueError as exc:
        assert "inventory changed" in str(exc)
    else:
        raise AssertionError("changed inventory was accepted")


def test_dispatcher_routes_only_matching_profile(tmp_path, monkeypatch):
    plane = build_plane(tmp_path, monkeypatch)
    plane.synchronizer.sync()
    estimate = plane.estimates.create({"selection": {"explicit_machine_ids": ["machine-standard"]},
        "start_at": "2026-01-01", "end_at": "2026-01-03", "feature_set_version": "v1",
        "standard_window_days": 1, "ulrpm_window_days": 1})
    plane.campaign_service.submit(estimate["estimate_id"], estimate["estimate_signature"], name="route",
        created_by="test", production=False, confirmation_text="", ulrpm_confirmation_text="")
    submitted = []
    class Adapter:
        def submit(self, item):
            assert item["cohort"] == "standard" and item["resource_profile_version"] == "standard-8g-v1"
            submitted.append(item)
            return WorkflowIdentity("workflow-1")
        def statuses(self): return {}
    dispatcher = Dispatcher(plane.campaigns, plane.inventory, plane.capacity, Adapter(), plane.pricing)
    assert dispatcher.tick() == 1
    assert len(submitted) == 1


def test_production_submission_is_blocked_by_global_readiness(tmp_path, monkeypatch):
    plane = build_plane(tmp_path, monkeypatch)
    plane.synchronizer.sync()
    estimate = plane.estimates.create({"selection": {"explicit_machine_ids": ["machine-standard"]},
        "start_at": "2026-01-01", "end_at": "2026-01-02", "feature_set_version": "v1",
        "standard_window_days": 1, "ulrpm_window_days": 1})
    try:
        plane.campaign_service.submit(estimate["estimate_id"], estimate["estimate_signature"], name="unsafe",
            created_by="test", production=True, confirmation_text="RUN_PROD_BACKFILL",
            ulrpm_confirmation_text="")
    except ValueError as exc:
        assert "production readiness failed" in str(exc)
    else:
        raise AssertionError("production campaign bypassed readiness")


def test_repeated_failures_auto_pause_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKFILL_AUTO_PAUSE_FAILURES", "2")
    plane = build_plane(tmp_path, monkeypatch)
    plane.synchronizer.sync()
    estimate = plane.estimates.create({"selection": {"explicit_machine_ids": ["machine-standard"]},
        "start_at": "2026-01-01", "end_at": "2026-01-03", "feature_set_version": "v1",
        "standard_window_days": 1, "ulrpm_window_days": 1})
    campaign = plane.campaign_service.submit(estimate["estimate_id"], estimate["estimate_signature"], name="pause",
        created_by="test", production=False, confirmation_text="", ulrpm_confirmation_text="")
    with plane.database.connect() as db:
        db.execute("UPDATE campaigns SET state='running' WHERE id=?", (campaign["id"],))
        db.execute("UPDATE work_items SET state='failed' WHERE campaign_id=?", (campaign["id"],))
    assert evaluate_campaign(plane.campaigns, campaign["id"])
    assert plane.campaigns.get(campaign["id"])["state"] == "paused"


def test_machine_pause_and_resume_controls_dispatch(tmp_path, monkeypatch):
    plane = build_plane(tmp_path, monkeypatch)
    plane.synchronizer.sync()
    estimate = plane.estimates.create({"selection": {"explicit_machine_ids": ["machine-standard"]},
        "start_at": "2026-01-01", "end_at": "2026-01-03", "feature_set_version": "v1",
        "standard_window_days": 1, "ulrpm_window_days": 1})
    campaign = plane.campaign_service.submit(estimate["estimate_id"], estimate["estimate_signature"], name="machine",
        created_by="test", production=False, confirmation_text="", ulrpm_confirmation_text="")
    apply_machine_action(plane.campaigns, campaign["id"], "machine-standard", "pause", "test", "investigate")
    assert plane.campaigns.lease("standard", 10, "worker") == []
    apply_machine_action(plane.campaigns, campaign["id"], "machine-standard", "resume", "test", "resolved")
    assert len(plane.campaigns.lease("standard", 10, "worker")) == 1


def test_inventory_sync_retries_transient_page_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("backfill_dashboard.control_plane.inventory_sync.time.sleep", lambda *_: None)
    plane = build_plane(tmp_path, monkeypatch)
    plane.synchronizer.source = FlakySource()
    result = plane.synchronizer.sync()
    assert result["status"] == "healthy"
    assert result["machine_count"] == 3


def test_database_migrates_preexisting_inventory_without_lifecycle_dates(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    import sqlite3
    legacy_schema = SCHEMA.replace(", last_recorded_at TEXT, installation_at TEXT", "")
    legacy_schema = legacy_schema.replace("is_test_machine INTEGER NOT NULL DEFAULT 0, ", "")
    sqlite3.connect(path).executescript(legacy_schema).close()
    Database(path)
    with sqlite3.connect(path) as connection:
        inventory_columns = {row[1] for row in connection.execute("PRAGMA table_info(inventory)")}
        stage_columns = {row[1] for row in connection.execute("PRAGMA table_info(inventory_stage)")}
    for columns in (inventory_columns, stage_columns):
        assert "last_recorded_at" in columns
        assert "installation_at" in columns
        assert "is_test_machine" in columns


def test_inventory_sync_round_trips_test_machine_flag(tmp_path, monkeypatch):
    plane = build_plane(tmp_path, monkeypatch)
    plane.synchronizer.source = FlakySource()
    plane.synchronizer.source.search = lambda query: MachinePage(
        [_record({"_id": "test-machine", "name": "ULRPM E2E test #5", "tags": ["ulrpm"]}, "source")],
        None,
        "v1",
    )

    result = plane.synchronizer.sync()

    assert result["status"] == "healthy"
    assert plane.inventory.get_many(["test-machine"])[0].is_test_machine is True
