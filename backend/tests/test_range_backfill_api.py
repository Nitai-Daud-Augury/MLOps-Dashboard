from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from backfill_dashboard.admin import AdminAction, AdminActionRepository
from backfill_dashboard.manifests import OrchestratedManifestWriteResult
from backfill_dashboard.schemas import OrchestratedBackfillRequest, OrchestratedManifestRequest


MACHINE = "6817571193e37ef05fffcd1c"


def _online_snapshot():
    return {"snapshot": {"machines": [{
        "machine_id": MACHINE,
        "months": [{
            "status": "needs_backfill",
            "activity_status": "online",
            "row_count": 10,
            "partition": {"year": 2026, "month": 3},
        }],
    }]}}


def test_create_orchestrated_manifests_uploads_exact_range(monkeypatch):
    import backfill_dashboard.app as app_module
    captured = {}

    class Writer:
        def write_orchestrated_monthly_manifests(self, **kwargs):
            captured.update(kwargs)
            return OrchestratedManifestWriteResult(
                account_name="a", container_name="c", manifest_prefix="p",
                manifest_template="p/month_{month_index:02d}.parquet",
                machine_ids=kwargs["machine_ids"], start_index=19, end_index=20,
                month_count=1, manifest_count=1, blob_url="https://example.invalid/p",
                month_indices_by_machine={MACHINE: [19]},
                windows_by_machine={MACHINE: [{
                    "month_index": 19, "since": "2026/03/01/00", "until": "2026/03/15/00",
                    "manifest_path": "p/monthly_x/month_19_2026_03.parquet",
                }]},
                split={"mode": "month", "days": None},
            )

    recorded = {}

    def record_completed(**kwargs):
        recorded.update(kwargs)
        return AdminAction(
            id="m1", action="manifest", status="succeeded",
            command=kwargs["command"], cwd=str(kwargs["cwd"]), created_at="now",
            machine_ids=kwargs.get("machine_ids") or [],
        )

    monkeypatch.setattr(app_module, "manifest_writer", Writer())
    monkeypatch.setattr(app_module.admin_repository, "record_completed", record_completed)
    monkeypatch.setattr(app_module.repository, "latest", _online_snapshot)

    result = app_module.create_orchestrated_manifests(OrchestratedManifestRequest(
        machine_ids=[MACHINE],
        since="2026-03-01T00:00:00Z",
        until="2026-03-15T00:00:00Z",
        month_indices_by_machine={MACHINE: [19]},
        split={"mode": "month"},
    ))
    assert captured["since"] == "2026-03-01T00:00:00Z"
    assert captured["until"] == "2026-03-15T00:00:00Z"
    assert result["manifest"]["manifest_count"] == 1
    assert result["manifest"]["windows"][MACHINE][0]["until"] == "2026/03/15/00"


def test_prepare_orchestrated_trigger_persists_manifest_windows(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module
    import backfill_dashboard.admin as admin_module

    monkeypatch.setattr(app_module, "BACKFILL_CANCEL_WINDOW_SECONDS", 0)
    actions = AdminActionRepository(tmp_path / "actions.json")

    class Writer:
        def write_orchestrated_monthly_manifests(self, **kwargs):
            return OrchestratedManifestWriteResult(
                account_name="a", container_name="c", manifest_prefix="p",
                manifest_template="p/month_{month_index:02d}.parquet",
                machine_ids=[MACHINE], start_index=19, end_index=20,
                month_count=1, manifest_count=1, blob_url="https://example.invalid/p",
                month_indices_by_machine={MACHINE: [19]},
                windows_by_machine={MACHINE: [{
                    "month_index": 19, "since": "2026/03/01/00", "until": "2026/03/15/00",
                    "manifest_path": "p/monthly_x/month_19_2026_03.parquet",
                }]},
                split={"mode": "month", "days": None},
            )

    class Builder:
        def build_ulrpm_orchestrator_trigger(self, **kwargs):
            return tmp_path, ["argo", "submit", "-p", "test=true"]

    monkeypatch.setattr(app_module, "admin_repository", actions)
    monkeypatch.setattr(app_module, "manifest_writer", Writer())
    monkeypatch.setattr(app_module, "command_builder", Builder())
    monkeypatch.setattr(app_module, "repository", SimpleNamespace(latest=_online_snapshot))
    monkeypatch.setattr(app_module, "_require_ulrpm_orchestrator_access", lambda: None)
    monkeypatch.setattr(app_module, "_reject_active_months", lambda *_: None)
    monkeypatch.setattr(app_module, "_reserve_months_for_action", lambda *_: None)
    monkeypatch.setattr(app_module, "_run_trigger_and_reconcile_reservations", lambda *_: None)
    monkeypatch.setattr(
        admin_module.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="Name: wf-1\n", stderr=""),
    )

    background = BackgroundTasks()
    response = app_module.trigger_orchestrated_machine_backfill(
        OrchestratedBackfillRequest(
            machine_ids=[MACHINE],
            since="2026-03-01T00:00:00Z",
            until="2026-03-15T00:00:00Z",
            month_indices_by_machine={MACHINE: [19]},
            split={"mode": "month"},
        ),
        background,
    )
    action_id = response["action"]["id"]
    task = background.tasks[0]
    task.func(*task.args, **task.kwargs)
    stored = actions.get(action_id)
    assert stored["manifest_windows"][MACHINE][0]["until"] == "2026/03/15/00"
    assert stored["split"]["mode"] == "month"


def test_replacement_reuses_stored_windows(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module
    from backfill_dashboard.control_store import JsonControlStore

    actions = AdminActionRepository(tmp_path / "actions.json")
    parent = actions.create(
        action="trigger", command=["argo"], cwd=tmp_path,
        machine_ids=[MACHINE], flow_name="UlrpmDevBackfillOrchestratorFlow",
    )
    actions.update_trigger(
        parent.id, phase="accepted",
        manifest_windows={MACHINE: [{
            "month_index": 19, "since": "2026/03/01/00", "until": "2026/03/15/00",
            "manifest_path": "p/month_19_2026_03.parquet",
        }]},
        split={"mode": "month", "days": None},
    )
    # Mark parent finished so wait loop exits.
    with actions._lock:
        actions._actions[parent.id].status = "succeeded"
        actions._actions[parent.id].workflow_id = f"action:{parent.id}"
        actions._persist()

    captured = {}

    class Writer:
        def write_orchestrated_monthly_manifests(self, **kwargs):
            captured.update(kwargs)
            return OrchestratedManifestWriteResult(
                account_name="a", container_name="c", manifest_prefix="p",
                manifest_template="p/t.parquet", machine_ids=[MACHINE],
                start_index=19, end_index=20, month_count=1, manifest_count=1,
                blob_url="https://example.invalid/p",
                month_indices_by_machine={MACHINE: [19]},
                windows_by_machine={MACHINE: captured.get("windows") or []},
                split={"mode": "month", "days": None},
            )

    class Builder:
        def build_ulrpm_orchestrator_trigger(self, **kwargs):
            return tmp_path, ["echo", "ok"]

    control = JsonControlStore(tmp_path / "control.json")
    monkeypatch.setattr(app_module, "admin_repository", actions)
    monkeypatch.setattr(app_module, "manifest_writer", Writer())
    monkeypatch.setattr(app_module, "command_builder", Builder())
    monkeypatch.setattr(app_module, "control_store", control)
    monkeypatch.setattr(app_module, "repository", SimpleNamespace(latest=_online_snapshot))
    monkeypatch.setattr(app_module, "_reject_non_online_months", lambda *_: None)
    monkeypatch.setattr(app_module.admin_repository, "run", lambda *_: None)
    monkeypatch.setattr(app_module.running_workflows, "list_running", lambda: {"workflows": []})
    monkeypatch.setattr(app_module.running_workflows, "lookup_workflows", lambda ids: [])
    monkeypatch.setattr(app_module.time, "sleep", lambda s: None)

    action_id = "replace-1"
    app_module.backfill_actions[action_id] = {
        "status": "queued", "events": [], "machine_id": MACHINE,
    }
    app_module._wait_and_submit_replacements(
        action_id, MACHINE, f"action:{parent.id}", [19], environment="dev",
    )
    assert captured["since"].startswith("2026-03-01")
    assert captured["until"].startswith("2026-03-15")
    assert app_module.backfill_actions[action_id]["status"] == "success"



def test_month_13_week_split_yields_five_windows_in_order():
    from backfill_dashboard.windows import SplitSpec, windows_for_selection

    windows = windows_for_selection(
        since="2025-09-01T00:00:00Z",
        until="2025-10-01T00:00:00Z",
        month_indices=[13],
        split=SplitSpec("week"),
    )
    assert len(windows) == 5
    since_days = [w.since.strftime("%m-%d") for w in windows]
    assert since_days == ["09-01", "09-08", "09-15", "09-22", "09-29"]
    for prev, curr in zip(windows, windows[1:]):
        assert prev.until == curr.since


def test_manifest_plan_serialization_preserves_list_order(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module
    import json

    plan = {
        MACHINE: [
            {"month_index": 13, "manifest": "w0.parquet", "since": "2025/09/01/00", "until": "2025/09/08/00"},
            {"month_index": 13, "manifest": "w1.parquet", "since": "2025/09/08/00", "until": "2025/09/15/00"},
            {"month_index": 13, "manifest": "w2.parquet", "since": "2025/09/15/00", "until": "2025/09/22/00"},
            {"month_index": 13, "manifest": "w3.parquet", "since": "2025/09/22/00", "until": "2025/09/29/00"},
            {"month_index": 13, "manifest": "w4.parquet", "since": "2025/09/29/00", "until": "2025/10/01/00"},
        ]
    }
    serialized = app_module._manifest_plan_from_windows({
        MACHINE: [
            {
                "month_index": item["month_index"],
                "since": item["since"],
                "until": item["until"],
                "manifest_path": item["manifest"],
            }
            for item in plan[MACHINE]
        ]
    })
    assert [item["since"] for item in serialized[MACHINE]] == [item["since"] for item in plan[MACHINE]]

    captured_cmd = {}

    class Builder:
        def build_ulrpm_orchestrator_trigger(self, **kwargs):
            captured_cmd.update(kwargs)
            # Mimic admin serialization
            import json as _json
            raw = _json.dumps(kwargs.get("manifest_plan"))
            parsed = _json.loads(raw)
            assert [item["since"][5:10] for item in parsed[MACHINE]] == [
                "09/01", "09/08", "09/15", "09/22", "09/29"
            ]
            return tmp_path, ["echo", "ok"]

    Builder().build_ulrpm_orchestrator_trigger(
        machine_ids=[MACHINE],
        manifest_template="t.parquet",
        start_index=13,
        end_index=14,
        params=__import__("backfill_dashboard.admin", fromlist=["TriggerParams"]).TriggerParams(),
        month_indices_by_machine={MACHINE: [13]},
        manifest_plan=serialized,
    )


def test_per_week_trigger_succeeds_with_v5_readiness(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module
    import backfill_dashboard.admin as admin_module

    monkeypatch.setattr(app_module, "BACKFILL_CANCEL_WINDOW_SECONDS", 0)
    actions = AdminActionRepository(tmp_path / "actions.json")
    week_windows = [
        {"month_index": 13, "since": f"2025/09/{d:02d}/00", "until": f"2025/09/{u:02d}/00",
         "manifest_path": f"p/w{i}.parquet"}
        for i, (d, u) in enumerate([(1, 8), (8, 15), (15, 22), (22, 29)])
    ] + [{
        "month_index": 13, "since": "2025/09/29/00", "until": "2025/10/01/00",
        "manifest_path": "p/w4.parquet",
    }]

    class Writer:
        def write_orchestrated_monthly_manifests(self, **kwargs):
            assert kwargs.get("split", {}).get("mode") == "week"
            return OrchestratedManifestWriteResult(
                account_name="a", container_name="c", manifest_prefix="p",
                manifest_template="p/month_{month_index:02d}.parquet",
                machine_ids=[MACHINE], start_index=13, end_index=14,
                month_count=1, manifest_count=5, blob_url="https://example.invalid/p",
                month_indices_by_machine={MACHINE: [13]},
                windows_by_machine={MACHINE: week_windows},
                split={"mode": "week", "days": None},
            )

    class Builder:
        def build_ulrpm_orchestrator_trigger(self, **kwargs):
            assert kwargs["manifest_plan"][MACHINE][0]["since"] == "2025/09/01/00"
            return tmp_path, ["echo", "ok"]

        def ulrpm_orchestrator_readiness(self):
            return {
                "ready": True,
                "supports_submonth_split": True,
                "contract_version": "2026-10-06-v5",
            }

    monkeypatch.setattr(app_module, "admin_repository", actions)
    monkeypatch.setattr(app_module, "manifest_writer", Writer())
    monkeypatch.setattr(app_module, "command_builder", Builder())
    monkeypatch.setattr(app_module, "repository", SimpleNamespace(latest=_online_snapshot))
    monkeypatch.setattr(app_module, "_require_ulrpm_orchestrator_access", lambda: None)
    monkeypatch.setattr(app_module, "_reject_active_months", lambda *_: None)
    monkeypatch.setattr(app_module, "_reserve_months_for_action", lambda *_: None)
    monkeypatch.setattr(app_module, "_run_trigger_and_reconcile_reservations", lambda *_: None)
    monkeypatch.setattr(app_module, "_reject_non_online_months", lambda *_: None)
    monkeypatch.setattr(
        admin_module.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="Name: wf-week\n", stderr=""),
    )

    # Expand online snapshot to include Sep 2025 month index 13
    def snap():
        return {"snapshot": {"machines": [{
            "machine_id": MACHINE,
            "months": [{
                "status": "needs_backfill",
                "activity_status": "online",
                "row_count": 10,
                "partition": {"year": 2025, "month": 9},
                "month_index": 13,
            }],
        }]}}

    monkeypatch.setattr(app_module, "repository", SimpleNamespace(latest=snap))

    background = BackgroundTasks()
    response = app_module.trigger_orchestrated_machine_backfill(
        OrchestratedBackfillRequest(
            machine_ids=[MACHINE],
            since="2025-09-01T00:00:00Z",
            until="2025-10-01T00:00:00Z",
            month_indices_by_machine={MACHINE: [13]},
            split={"mode": "week"},
        ),
        background,
    )
    action_id = response["action"]["id"]
    task = background.tasks[0]
    task.func(*task.args, **task.kwargs)
    stored = actions.get(action_id)
    assert stored["split"]["mode"] == "week"
    assert len(stored["manifest_windows"][MACHINE]) == 5


def test_per_week_trigger_rejected_with_v4_readiness(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module
    from fastapi import HTTPException

    class Builder:
        def ulrpm_orchestrator_readiness(self):
            return {
                "ready": True,
                "supports_submonth_split": False,
                "contract_version": "2026-09-17-v4",
            }

    monkeypatch.setattr(app_module, "command_builder", Builder())
    monkeypatch.setattr(app_module, "_require_ulrpm_orchestrator_access", lambda: None)
    monkeypatch.setattr(app_module, "_reject_non_online_months", lambda *_: None)
    monkeypatch.setattr(app_module, "repository", SimpleNamespace(latest=_online_snapshot))

    # _validate_selected_indices_for_queue / online months — stub soft
    monkeypatch.setattr(app_module, "_validate_selected_indices_for_queue", lambda *_: None)

    try:
        app_module.trigger_orchestrated_machine_backfill(
            OrchestratedBackfillRequest(
                machine_ids=[MACHINE],
                since="2025-09-01T00:00:00Z",
                until="2025-10-01T00:00:00Z",
                month_indices_by_machine={MACHINE: [13]},
                split={"mode": "week"},
            ),
            BackgroundTasks(),
        )
        assert False, "expected rejection for v4"
    except (HTTPException, ValueError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        assert "v5" in str(detail).lower() or "submonth" in str(detail).lower() or "Split" in str(detail)


def test_replacement_refuses_while_child_running(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module
    from backfill_dashboard.control_store import JsonControlStore

    actions = AdminActionRepository(tmp_path / "actions.json")
    parent = actions.create(
        action="trigger", command=["argo"], cwd=tmp_path,
        machine_ids=[MACHINE], flow_name="UlrpmDevBackfillOrchestratorFlow",
    )
    with actions._lock:
        actions._actions[parent.id].status = "succeeded"
        actions._actions[parent.id].workflow_id = f"action:{parent.id}"
        actions._persist()

    control = JsonControlStore(tmp_path / "control.json")
    control.request_cancel(
        MACHINE, 19, f"action:{parent.id}", "argo-fstbackfill-child-keep", "tester",
    )

    submitted = {"count": 0}

    class Writer:
        def write_orchestrated_monthly_manifests(self, **kwargs):
            submitted["count"] += 1
            raise AssertionError("must not write manifests while child running")

    class Builder:
        def build_ulrpm_orchestrator_trigger(self, **kwargs):
            raise AssertionError("must not submit while child running")

    clock = {"t": 0.0}

    def fake_monotonic():
        return clock["t"]

    def lookup(ids):
        clock["t"] += 10**7
        return [{"workflow_id": ids[0], "status": "Running"}]

    monkeypatch.setattr(app_module, "monotonic", fake_monotonic)
    monkeypatch.setattr(app_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(app_module.running_workflows, "lookup_workflows", lookup)
    monkeypatch.setattr(
        app_module.running_workflows, "list_running",
        lambda: {"workflows": []},
    )
    monkeypatch.setattr(app_module, "admin_repository", actions)
    monkeypatch.setattr(app_module, "manifest_writer", Writer())
    monkeypatch.setattr(app_module, "command_builder", Builder())
    monkeypatch.setattr(app_module, "control_store", control)
    monkeypatch.setattr(app_module, "repository", SimpleNamespace(latest=_online_snapshot))
    monkeypatch.setattr(app_module, "_reject_non_online_months", lambda *_: None)

    action_id = "replace-refuse"
    app_module.backfill_actions[action_id] = {
        "status": "queued", "events": [], "machine_id": MACHINE,
    }
    app_module._wait_and_submit_replacements(
        action_id, MACHINE, f"action:{parent.id}", [19], environment="dev",
    )
    assert app_module.backfill_actions[action_id]["status"] == "failed"
    assert "child" in (app_module.backfill_actions[action_id].get("error") or "").lower() or \
        "FSTBackfill" in (app_module.backfill_actions[action_id].get("error") or "")
    assert submitted["count"] == 0


def test_replacement_submits_after_child_finishes(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module
    from backfill_dashboard.control_store import JsonControlStore

    actions = AdminActionRepository(tmp_path / "actions.json")
    parent = actions.create(
        action="trigger", command=["argo"], cwd=tmp_path,
        machine_ids=[MACHINE], flow_name="UlrpmDevBackfillOrchestratorFlow",
    )
    actions.update_trigger(
        parent.id, phase="accepted",
        manifest_windows={MACHINE: [{
            "month_index": 19, "since": "2026/03/01/00", "until": "2026/03/15/00",
            "manifest_path": "p/month_19_2026_03.parquet",
        }]},
        split={"mode": "month", "days": None},
    )
    with actions._lock:
        actions._actions[parent.id].status = "succeeded"
        actions._actions[parent.id].workflow_id = f"action:{parent.id}"
        actions._persist()

    control = JsonControlStore(tmp_path / "control.json")
    control.request_cancel(
        MACHINE, 19, f"action:{parent.id}", "argo-fstbackfill-child-done", "tester",
    )

    captured = {}
    lookups = {"n": 0}

    class Writer:
        def write_orchestrated_monthly_manifests(self, **kwargs):
            captured.update(kwargs)
            return OrchestratedManifestWriteResult(
                account_name="a", container_name="c", manifest_prefix="p",
                manifest_template="p/t.parquet", machine_ids=[MACHINE],
                start_index=19, end_index=20, month_count=1, manifest_count=1,
                blob_url="https://example.invalid/p",
                month_indices_by_machine={MACHINE: [19]},
                windows_by_machine={MACHINE: [{
                    "month_index": 19, "since": "2026/03/01/00", "until": "2026/03/15/00",
                    "manifest_path": "p/month_19_2026_03.parquet",
                }]},
                split={"mode": "month", "days": None},
            )

    class Builder:
        def build_ulrpm_orchestrator_trigger(self, **kwargs):
            return tmp_path, ["echo", "ok"]

    def lookup(ids):
        lookups["n"] += 1
        if lookups["n"] == 1:
            return [{"workflow_id": ids[0], "status": "Running"}]
        return [{"workflow_id": ids[0], "status": "Succeeded"}]

    monkeypatch.setattr(app_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(app_module.running_workflows, "lookup_workflows", lookup)
    monkeypatch.setattr(app_module.running_workflows, "list_running", lambda: {"workflows": []})
    monkeypatch.setattr(app_module, "admin_repository", actions)
    monkeypatch.setattr(app_module, "manifest_writer", Writer())
    monkeypatch.setattr(app_module, "command_builder", Builder())
    monkeypatch.setattr(app_module, "control_store", control)
    monkeypatch.setattr(app_module, "repository", SimpleNamespace(latest=_online_snapshot))
    monkeypatch.setattr(app_module, "_reject_non_online_months", lambda *_: None)
    monkeypatch.setattr(app_module.admin_repository, "run", lambda *_: None)

    action_id = "replace-ok"
    app_module.backfill_actions[action_id] = {
        "status": "queued", "events": [], "machine_id": MACHINE,
    }
    app_module._wait_and_submit_replacements(
        action_id, MACHINE, f"action:{parent.id}", [19], environment="dev",
    )
    assert lookups["n"] >= 2
    assert captured.get("since", "").startswith("2026-03-01")
    assert app_module.backfill_actions[action_id]["status"] == "success"
