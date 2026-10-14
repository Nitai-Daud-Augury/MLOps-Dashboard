"""T7: a terminated/failed/cancelled run must not keep a busy lane in runner-lock-status (read-only path)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backfill_dashboard import month_locks
from backfill_dashboard.admin import AdminActionRepository
from backfill_dashboard.control_store import JsonControlStore
from backfill_dashboard.runtime_mode import runtime_info

MACHINE = "67ed190c99a0ea11cd3dced5"
PARENT = "fstbkfill.test.devfstm.ulrpmdeatorflow-hwqbr-s2skp"
TOKEN = "ghp_" + "x" * 36


@pytest.mark.parametrize("status", ["Succeeded", "Failed", "Error", "terminated", "cancelled", "Canceled", "aborted", "timed_out"])
def test_terminal_statuses(status):
    assert month_locks.is_terminal(status)


@pytest.mark.parametrize("status", ["Running", "Pending", "queued", "accepted", "", None])
def test_active_statuses(status):
    assert not month_locks.is_terminal(status)


class _Provider:
    def __init__(self, workflows):
        self.workflows = workflows

    def list_running(self):
        return {"workflows": list(self.workflows)}


@pytest.fixture()
def lane(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module

    control = JsonControlStore(tmp_path / "control.json")
    repository = AdminActionRepository(tmp_path / "actions.json")
    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info({"MLOPS_DASHBOARD_RUNTIME": "databricks", "GITHUB_DISPATCH_TOKEN": TOKEN}))
    monkeypatch.setattr(app_module, "control_store", control)
    monkeypatch.setattr(app_module, "admin_repository", repository)
    monkeypatch.setattr(app_module, "_release_stale_submission_reservations", lambda: None)
    inventory = SimpleNamespace(get_many=lambda ids: [SimpleNamespace(machine_id=i) for i in ids])
    monkeypatch.setattr(app_module, "control_plane", SimpleNamespace(inventory=inventory))

    def trigger(status, workflow_id=None):
        action = repository.create(action="trigger", command=["true"], cwd=tmp_path, machine_ids=[MACHINE])
        with repository._lock:
            repository._actions[action.id].status = status
            repository._actions[action.id].workflow_id = workflow_id
            repository._persist()
        return action.id

    def busy(workflows):
        monkeypatch.setattr(app_module, "running_workflows", _Provider(workflows))
        return app_module.runner_lock_status()["busy_machines"]

    return SimpleNamespace(control=control, trigger=trigger, busy=busy)


def test_cancelled_submission_without_workflow_frees_the_lane(lane):
    action_id = lane.trigger("cancelled")
    lane.control.reserve_month(MACHINE, 8, f"action:{action_id}", "dashboard")
    assert lane.busy([]) == {}


def test_terminated_parent_still_listed_frees_the_lane(lane):
    lane.trigger("running", PARENT)
    lane.control.reserve_month(MACHINE, 8, "action:x", "dashboard")
    lane.control.link_parent_workflow(MACHINE, 8, PARENT)
    assert lane.busy([{"workflow_id": PARENT, "status": "Failed", "machine_ids": [MACHINE]}]) == {}


def test_running_parent_keeps_the_lane(lane):
    lane.trigger("running", PARENT)
    lane.control.reserve_month(MACHINE, 8, "action:x", "dashboard")
    lane.control.link_parent_workflow(MACHINE, 8, PARENT)
    assert MACHINE in lane.busy([{"workflow_id": PARENT, "status": "Running", "machine_ids": [MACHINE]}])
