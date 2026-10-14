"""Regression: terminated/failed/cancelled runs must release their month lock (BUG 1).

Reproduces 67ed190c99a0ea11cd3dced5 / 2025-04 (month_index 8): reserved queued, linked to
parent ulrpm-...-s2skp, the parent terminated through GitHub Actions; the record stayed
``queued`` and every new backfill failed with "A selected month already has an active backfill".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backfill_dashboard import month_locks
from backfill_dashboard.admin import AdminActionRepository
from backfill_dashboard.control_store import JsonControlStore
from backfill_dashboard.outerbounds_gha import GithubActionsRunningWorkflowProvider
from backfill_dashboard.runtime_mode import runtime_info
from backfill_dashboard.schemas import ReleaseMonthLockRequest

MACHINE = "67ed190c99a0ea11cd3dced5"
MONTH = 8  # 2025-04
PARENT = "fstbkfill.test.devfstm.ulrpmdeatorflow-hwqbr-s2skp"
TOKEN = "ghp_" + "x" * 36
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _record(state="queued", parent=PARENT, updated=NOW - timedelta(minutes=10), **extra):
    return {"machine_id": MACHINE, "month_index": MONTH, "year": 2025, "month": 4, "state": state,
            "parent_workflow_id": parent, "updated_at": updated.isoformat(), "events": [], **extra}


def _wf(status, workflow_id=PARENT):
    return {workflow_id: {"workflow_id": workflow_id, "status": status}}


# --- the rule set -----------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [("Failed", "failed"), ("Error", "failed"), ("Succeeded", "completed")])
def test_terminal_snapshot_releases(status, expected):
    release = month_locks.decide(_record(), workflows_by_id=_wf(status), now=NOW)
    assert release and release.state == expected and release.source == "snapshot"


def test_terminated_parent_is_cancelled_even_when_the_cached_snapshot_still_says_running():
    release = month_locks.decide(_record(), workflows_by_id=_wf("Running"), terminated_ids=[f"argo-{PARENT}"], now=NOW)
    assert release and release.state == "cancelled"


def test_absent_from_snapshot_released_only_when_known_terminal():
    assert month_locks.decide(_record(), workflows_by_id={}, now=NOW, ttl_seconds=86400) is None
    by_terminate = month_locks.decide(_record(), workflows_by_id={}, terminated_ids=[PARENT], now=NOW)
    assert by_terminate.state == "cancelled" and by_terminate.source == "terminate"
    by_action = month_locks.decide(_record(), workflows_by_id={}, terminal_ids=[PARENT], now=NOW)
    assert by_action.state == "failed" and by_action.source == "known_terminal"


def test_ttl_safety_net_never_releases_an_active_run():
    old = _record(updated=NOW - timedelta(days=3))
    assert month_locks.decide(old, workflows_by_id=_wf("Running"), now=NOW, ttl_seconds=3600) is None
    stale = month_locks.decide(old, workflows_by_id={}, now=NOW, ttl_seconds=3600)
    assert stale.state == "failed" and stale.source == "stale_ttl"
    assert month_locks.decide(old, workflows_by_id={}, now=NOW, ttl_seconds=0) is None  # 0 disables


def test_active_child_keeps_the_lock_and_inflight_submissions_and_requeues_are_left_alone():
    child_running = _record(child_workflow_id="fstbackfill-kx9ds")
    workflows = {**_wf("Failed"), **_wf("Running", "fstbackfill-kx9ds")}
    assert month_locks.decide(child_running, workflows_by_id=workflows, now=NOW) is None
    assert month_locks.decide(_record(parent="action:abc"), workflows_by_id={}, terminal_ids=["action:abc"], now=NOW,
                              ttl_seconds=900) is None
    requeue = _record(state="requeue_pending")
    assert month_locks.decide(requeue, workflows_by_id=_wf("Failed"), now=NOW) is None
    assert month_locks.decide(_record(state="completed"), workflows_by_id=_wf("Failed"), now=NOW) is None


def test_ttl_setting(monkeypatch):
    assert month_locks.stale_ttl_seconds({}) == 24 * 3600
    assert month_locks.stale_ttl_seconds({month_locks.STALE_TTL_ENV: "0"}) == 0
    assert month_locks.stale_ttl_seconds({month_locks.STALE_TTL_ENV: "60"}) == 900
    assert month_locks.stale_ttl_seconds({month_locks.STALE_TTL_ENV: "junk"}) == 24 * 3600


def test_action_ledger_sets():
    terminated, terminal = month_locks.action_terminal_sets([
        {"action": "terminate", "status": "succeeded", "workflow_id": PARENT},
        {"action": "terminate", "status": "failed", "workflow_id": "wf-x"},
        {"action": "trigger", "status": "failed", "phase": "accepted", "workflow_id": "wf-a"},
        {"action": "trigger", "status": "running", "phase": "accepted", "workflow_id": "wf-b"},
    ])
    assert terminated == {PARENT} and terminal == {"wf-a"}


# --- app wiring -------------------------------------------------------------------------------

class _Provider:
    def __init__(self, workflows=None, fetched=True):
        self.workflows = list(workflows or [])
        self.fetched = fetched

    def lookup_workflows(self, ids):
        wanted = {item.removeprefix("argo-") for item in ids}
        return [w for w in self.workflows if w["workflow_id"] in wanted]

    def list_recent(self):
        return {"workflows": list(self.workflows), "fetched_at": "2026-10-12T09:20:20Z" if self.fetched else None}

    list_running = list_recent


@pytest.fixture()
def ctx(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module

    control = JsonControlStore(tmp_path / "control.json")
    control.reserve_month(MACHINE, MONTH, "action:a1", "dashboard")
    control.link_parent_workflow(MACHINE, MONTH, PARENT)
    repository = AdminActionRepository(tmp_path / "actions.json")
    trigger = repository.create(action="trigger", command=["true"], cwd=tmp_path, machine_ids=[MACHINE])
    with repository._lock:
        repository._actions[trigger.id].status = "running"
        repository._actions[trigger.id].phase = "accepted"
        repository._actions[trigger.id].workflow_id = PARENT
        repository._persist()
    provider = _Provider()
    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info({"MLOPS_DASHBOARD_RUNTIME": "databricks", "GITHUB_DISPATCH_TOKEN": TOKEN}))
    monkeypatch.setattr(app_module, "control_store", control)
    monkeypatch.setattr(app_module, "admin_repository", repository)
    monkeypatch.setattr(app_module, "running_workflows", provider)
    monkeypatch.setattr(app_module, "status_refresh_gate", SimpleNamespace(request=lambda *a, **k: {}, status=lambda *a: {}))
    return SimpleNamespace(app=app_module, control=control, repository=repository, provider=provider)


def _record_terminate(repository, tmp_path, status="succeeded"):
    action = repository.create(action="terminate", command=["true"], cwd=tmp_path)
    with repository._lock:
        repository._actions[action.id].status = status
        repository._actions[action.id].workflow_id = PARENT
        repository._persist()
    return repository.get(action.id)


def test_bug_repro_queued_lock_absent_from_snapshot_blocked_the_next_backfill(ctx):
    # Before the fix the guard only read the record: queued -> rejected forever.
    with pytest.raises(ValueError, match="already has an active backfill"):
        ctx.app._reject_active_months({MACHINE: [MONTH]})
    ctx.provider.workflows = [{"workflow_id": PARENT, "status": "Failed"}]  # snapshot after the terminate
    ctx.app._reject_active_months({MACHINE: [MONTH]})  # no longer raises
    record = ctx.control.get_month_state(MACHINE, MONTH)
    assert record["state"] == "failed" and record["events"][-1]["source"] == "snapshot"


def test_terminate_success_releases_the_lock_immediately(ctx, tmp_path):
    ctx.provider.workflows = [{"workflow_id": PARENT, "status": "Running"}]  # stale cached snapshot
    action = _record_terminate(ctx.repository, tmp_path)
    ctx.app._refresh_on_dispatch_completion(action)
    record = ctx.control.get_month_state(MACHINE, MONTH)
    assert record["state"] == "cancelled"
    assert record["events"][-1]["source"] in {"snapshot", "terminate"} and record["events"][-1]["workflow_id"] == PARENT
    ctx.app._reject_active_months({MACHINE: [MONTH]})


def test_failed_terminate_keeps_the_lock(ctx, tmp_path):
    ctx.app._refresh_on_dispatch_completion(_record_terminate(ctx.repository, tmp_path, status="failed"))
    assert ctx.control.get_month_state(MACHINE, MONTH)["state"] == "queued"


def test_gha_snapshot_hook_reconciles_locks(ctx, monkeypatch):
    def runner(action, request_id, inputs, **kwargs):
        return {"ok": True, "workflows": [{"metadata": {"name": PARENT, "annotations": {"metaflow/flow_name": "UlrpmDevBackfillOrchestratorFlow"}},
                                           "status": {"phase": "Failed"}}]}

    provider = GithubActionsRunningWorkflowProvider(environ={"GITHUB_DISPATCH_TOKEN": TOKEN}, runner=runner,
                                                    start_thread=lambda target: target())
    provider.on_snapshot.append(ctx.app._reconcile_after_snapshot)
    assert provider.start_refresh()
    assert ctx.control.get_month_state(MACHINE, MONTH)["state"] == "failed"
    assert ctx.repository.get(ctx.repository.list()[0]["id"])["status"] == "failed"  # ledger synced too


def test_stale_ttl_releases_when_the_run_vanished(ctx, monkeypatch):
    monkeypatch.setenv(month_locks.STALE_TTL_ENV, "900")
    # The parent is not in the snapshot and nothing says it is terminal: only the TTL applies.
    with ctx.repository._lock:
        for action in ctx.repository._actions.values():
            action.workflow_id = "other-wf"
    records = ctx.control._load()
    key = f"{MACHINE}:{MONTH}"
    records[key]["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    ctx.control._save(records)
    ctx.app._reject_active_months({MACHINE: [MONTH]})
    assert ctx.control.get_month_state(MACHINE, MONTH)["events"][-1]["source"] == "stale_ttl"


# --- admin release endpoint -------------------------------------------------------------------

def _request(user="nitai@augury.com"):
    return SimpleNamespace(headers={"x-forwarded-email": user}, client=None)


def test_admin_release_refuses_active_runs_and_requires_confirm(ctx):
    ctx.provider.workflows = [{"workflow_id": PARENT, "status": "Running"}]
    with pytest.raises(HTTPException) as active:
        ctx.app.release_month_lock(MACHINE, MONTH, ReleaseMonthLockRequest(dry_run=False, confirm=f"{MACHINE}:2025-04", reason="x" * 5), _request())
    assert active.value.status_code == 409 and "Running" in active.value.detail
    ctx.provider.workflows = []
    with pytest.raises(HTTPException) as no_snapshot:
        ctx.provider.fetched = False
        ctx.app.release_month_lock(MACHINE, MONTH, ReleaseMonthLockRequest(), _request())
    assert no_snapshot.value.status_code == 409
    ctx.provider.fetched = True
    with pytest.raises(HTTPException) as pending:  # dispatched minutes ago, not in the snapshot yet
        ctx.app.release_month_lock(MACHINE, MONTH, ReleaseMonthLockRequest(), _request())
    assert pending.value.status_code == 409 and "dispatched recently" in pending.value.detail
    ctx.provider.workflows = [{"workflow_id": PARENT, "status": "Failed"}]
    with pytest.raises(HTTPException) as wrong:
        ctx.app.release_month_lock(MACHINE, MONTH, ReleaseMonthLockRequest(dry_run=False, confirm=MACHINE, reason="stale"), _request())
    assert wrong.value.status_code == 400
    assert ctx.control.get_month_state(MACHINE, MONTH)["state"] == "queued"


def test_admin_release_dry_run_then_audited_release(ctx):
    ctx.provider.workflows = [{"workflow_id": PARENT, "status": "Failed"}]
    preview = ctx.app.release_month_lock(MACHINE, MONTH, ReleaseMonthLockRequest(), _request())
    assert preview["dry_run"] and preview["would_change"]["from_state"] == "queued" and preview["would_change"]["to_state"] == "cancelled"
    assert ctx.control.get_month_state(MACHINE, MONTH)["state"] == "queued"  # dry run wrote nothing
    done = ctx.app.release_month_lock(MACHINE, MONTH, ReleaseMonthLockRequest(
        dry_run=False, confirm=f"{MACHINE}:2025-04", reason="parent s2skp terminated"), _request())
    record = ctx.control.get_month_state(MACHINE, MONTH)
    event = record["events"][-1]
    assert record["state"] == "cancelled" and event["actor"] == "nitai@augury.com" and event["source"] == "admin_release"
    assert event["reason"] == "parent s2skp terminated" and event["released_from"] == "queued"
    audit = ctx.repository.get(done["audit_action_id"])
    assert audit["action"] == "release_lock" and audit["machine_ids"] == [MACHINE]
    with pytest.raises(HTTPException) as again:
        ctx.app.release_month_lock(MACHINE, MONTH, ReleaseMonthLockRequest(), _request())
    assert again.value.status_code == 409
