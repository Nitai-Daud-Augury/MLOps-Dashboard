"""Latency fixes for the GitHub Actions backend: pending rows, cache invalidation, fast poll."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backfill_dashboard import pending_runs
from backfill_dashboard.outerbounds_gha import GithubActionsRunningWorkflowProvider
from backfill_dashboard.runtime_mode import runtime_info
from backfill_dashboard.status_refresh import StatusRefreshGate, is_dispatch_completion

TOKEN = "github_pat_" + "x" * 40  # fake
NOW = datetime(2026, 10, 11, 19, 0, tzinfo=timezone.utc)
FLOW = "UlrpmDevBackfillOrchestratorFlow"
WF = "fstbkfill.test.devfstm.ulrpmdeatorflow-hwqbr-fvn94"
GH_URL = "https://github.com/Nitai-Daud-Augury/MLOps-Dashboard/actions/runs/38165672718"


def _action(**overrides) -> dict:
    base = {"id": "a84d11a32de04250bd458f455cd8b3ee", "action": "trigger", "status": "running", "phase": "accepted",
            "created_at": (NOW - timedelta(minutes=2)).isoformat(), "flow_name": FLOW, "workflow_id": WF,
            "machine_ids": ["machine-a"], "stdout": f"Workflow triggered via GitHub Actions ({GH_URL}).\nName: {WF}\n"}
    return {**base, **overrides}


def test_pending_rows_cover_each_dispatch_stage():
    actions = [
        _action(id="q" * 32, phase="cancel_window", status="queued", workflow_id=None, stdout=""),
        _action(id="d" * 32, phase="submitting", workflow_id=None, stdout=""),
        _action(),
    ]
    rows = pending_runs.pending_rows(actions, [], now=NOW)
    assert [row["status"] for row in rows] == ["Queued", "Dispatching", "Submitted"]
    assert rows[0]["workflow_id"] == "pending-qqqqqqqqqqqq" and rows[0]["outerbounds_url"] == ""
    submitted = rows[2]
    assert submitted["workflow_id"] == WF and submitted["pending"] is True
    assert submitted["machine_ids"] == ["machine-a"] and submitted["github_run_url"] == GH_URL
    assert submitted["outerbounds_url"].endswith(f"/{FLOW}/argo-{WF}/view/run")
    assert all(row["finished_at"] is None for row in rows)  # never counted as completed/failed


def test_pending_rows_disappear_once_the_status_source_knows_the_workflow():
    assert pending_runs.pending_rows([_action()], [WF], now=NOW) == []
    assert pending_runs.pending_rows([_action()], [f"argo-{WF}"], now=NOW) == []
    assert pending_runs.expected_workflow_ids([_action()], [], now=NOW) == [WF]
    assert pending_runs.expected_workflow_ids([_action()], [WF], now=NOW) == []


@pytest.mark.parametrize("overrides", [
    {"status": "succeeded"}, {"status": "failed"}, {"action": "terminate"},
    {"created_at": (NOW - timedelta(hours=2)).isoformat()}, {"created_at": None},
])
def test_pending_rows_ignore_finished_old_or_non_trigger_actions(overrides):
    assert pending_runs.pending_rows([_action(**overrides)], [], now=NOW) == []


def test_pending_window_env_is_bounded():
    assert pending_runs.window_seconds({}) == 1800
    assert pending_runs.window_seconds({"OUTERBOUNDS_PENDING_WINDOW_SECONDS": "5"}) == 60
    assert pending_runs.window_seconds({"OUTERBOUNDS_PENDING_WINDOW_SECONDS": "bad"}) == 1800


class _Harness:
    """GitHub Actions status provider + refresh gate with a fake runner, threads and clocks."""

    def __init__(self, env: dict | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.threads: list = []
        self.timers: list[tuple[float, object]] = []
        self.mono = [0.0]
        self.wall = [1_000_000.0]
        environ = {"GITHUB_DISPATCH_TOKEN": TOKEN, **(env or {})}
        self.provider = GithubActionsRunningWorkflowProvider(
            environ=environ, runner=self._runner,
            start_thread=self.threads.append, clock=lambda: self.mono[0], wall=lambda: self.wall[0])
        self.gate = StatusRefreshGate(self.provider, environ=environ, clock=lambda: self.mono[0],
                                      timer=lambda delay, fn: self.timers.append((delay, fn)), log=lambda m: None)

    def _runner(self, action, request_id, inputs, **_kwargs):
        self.calls.append((action, request_id, dict(inputs)))
        return {"ok": True, "workflows": [_wf(WF)]}

    def finish(self):
        while self.threads:
            self.threads.pop(0)()

    def advance(self, seconds: float):
        self.mono[0] += seconds
        self.wall[0] += seconds


def _wf(name: str, phase: str = "Running") -> dict:
    return {"metadata": {"name": name}, "status": {"phase": phase}}


def test_manual_refresh_joins_in_flight_then_cools_down_globally():
    h = _Harness()
    assert h.gate.request("a@x.com")["state"] == "started"
    assert h.gate.request("a@x.com")["state"] == "joined"
    assert h.gate.request("b@x.com")["state"] == "joined"  # concurrent requests join the one run
    h.advance(80)
    h.finish()  # the status run took 80s
    limited = h.gate.request("b@x.com")
    assert limited == {"state": "limited", "retry_after": 60, "reason": "manual"}  # cooldown from completion
    h.advance(61)
    assert h.gate.request("b@x.com")["state"] == "started"
    h.finish()
    assert len(h.calls) == 2


def test_spamming_refresh_never_creates_extra_status_runs():
    h = _Harness()
    states = [h.gate.request(f"user{n % 7}@x.com")["state"] for n in range(200)]
    assert states[0] == "started" and set(states[1:]) == {"joined"}
    h.finish()
    for _ in range(200):
        h.advance(0.1)
        assert h.gate.request("spammer@x.com")["state"] == "limited"
    assert len(h.threads) == 0 and len(h.calls) == 1


def test_per_user_cooldown_is_separate_from_the_global_one():
    h = _Harness({"WORKFLOW_STATUS_REFRESH_COOLDOWN_SECONDS": "10", "WORKFLOW_STATUS_REFRESH_USER_COOLDOWN_SECONDS": "60"})
    assert h.gate.request("A@x.com")["state"] == "started"
    h.advance(1)
    h.finish()
    h.advance(19)
    assert h.gate.request("a@x.com") == {"state": "limited", "retry_after": 40, "reason": "manual"}  # case-insensitive user
    assert h.gate.request("b@x.com")["state"] == "started"
    assert h.gate.status("a@x.com")["retry_after"] == 40


def test_dispatch_completion_event_refreshes_once_and_defers_inside_the_cooldown():
    h = _Harness()
    assert h.gate.request("dispatch-event", reason="event")["state"] == "started"
    assert h.gate.request("dispatch-event", reason="event")["state"] == "joined"
    h.advance(70)
    h.finish()
    h.advance(10)
    deferred = h.gate.request("dispatch-event", reason="event")
    assert deferred["state"] == "deferred" and deferred["retry_after"] == 50
    assert h.gate.request("dispatch-event", reason="event")["state"] == "deferred"
    assert len(h.timers) == 1  # one deferred refresh, however many events
    h.advance(50)
    h.timers.pop()[1]()
    assert h.provider.refreshing is True and len(h.threads) == 1
    h.finish()
    assert len(h.calls) == 2


def test_cooldown_env_is_bounded_and_defaults_to_60():
    assert StatusRefreshGate(object(), environ={}).cooldown == 60
    assert StatusRefreshGate(object(), environ={"WORKFLOW_STATUS_REFRESH_COOLDOWN_SECONDS": "1"}).cooldown == 10
    assert StatusRefreshGate(object(), environ={"WORKFLOW_STATUS_REFRESH_USER_COOLDOWN_SECONDS": "x"}).user_cooldown == 60


def test_is_dispatch_completion():
    assert is_dispatch_completion({"action": "trigger", "workflow_id": WF, "status": "running"})
    assert not is_dispatch_completion({"action": "trigger", "workflow_id": None, "status": "failed"})
    assert is_dispatch_completion({"action": "terminate", "status": "succeeded"})
    assert not is_dispatch_completion({"action": "terminate", "status": "failed"})
    assert not is_dispatch_completion({"action": "logs", "status": "succeeded"})


def test_finished_admin_action_fires_the_completion_hook(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from backfill_dashboard.admin import AdminActionRepository

    repository = AdminActionRepository(tmp_path / "actions.json")
    seen: list[dict] = []
    repository.on_finished.append(seen.append)
    repository.on_finished.append(lambda action: (_ for _ in ()).throw(RuntimeError("hook bug")))  # must not break run()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=f"Name: {WF}\n", stderr=""))
    action = repository.create(action="trigger", command=["true"], cwd=tmp_path, machine_ids=["machine-a"])
    repository.run(action.id)
    assert seen and seen[0]["workflow_id"] == WF and is_dispatch_completion(seen[0])


def test_refresh_endpoint_is_rate_limited_and_never_multiplies_runs(monkeypatch):
    from types import SimpleNamespace

    from backfill_dashboard import app as app_module

    h = _Harness()
    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info(
        {"MLOPS_DASHBOARD_RUNTIME": "databricks", "GITHUB_DISPATCH_TOKEN": TOKEN}))
    monkeypatch.setattr(app_module, "running_workflows", h.provider)
    monkeypatch.setattr(app_module, "status_refresh_gate", h.gate)
    monkeypatch.setattr(app_module.admin_repository, "list", lambda: [])
    monkeypatch.setattr(app_module.admin_repository, "sync_workflow_statuses", lambda workflows: None)
    request = SimpleNamespace(headers={"x-forwarded-email": "nitai@augury.com"}, client=None)
    first = app_module.refresh_workflow_status(request)
    assert first["refresh"]["state"] == "started" and first["refreshing"] is True
    for _ in range(20):
        assert app_module.refresh_workflow_status(request)["refresh"]["state"] == "joined"
    h.finish()
    limited = app_module.refresh_workflow_status(request)
    assert limited["refresh"]["state"] == "limited" and limited["refresh"]["retry_after"] == 60
    assert [item["workflow_id"] for item in limited["workflows"]] == [WF]  # cached snapshot is returned
    assert len(h.calls) == 1


def test_summary_and_machine_view_include_pending_dispatches(monkeypatch):
    from backfill_dashboard import app as app_module

    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info(
        {"MLOPS_DASHBOARD_RUNTIME": "databricks", "GITHUB_DISPATCH_TOKEN": TOKEN}))
    assert app_module.RUNTIME_INFO.workflow_backend == "github_actions"
    action = _action(created_at=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(app_module.admin_repository, "list", lambda: [action])
    monkeypatch.setattr(app_module.admin_repository, "for_machine", lambda machine_id: [action])
    monkeypatch.setattr(app_module.admin_repository, "active_workflow_ids", lambda machine_id=None: [WF])
    monkeypatch.setattr(app_module.admin_repository, "sync_workflow_statuses", lambda workflows: None)
    monkeypatch.setattr(app_module.running_workflows, "list_recent", lambda: {"workflows": [], "counts": {}})
    monkeypatch.setattr(app_module.running_workflows, "lookup_workflows", lambda ids: [])

    summary = app_module._with_pending_dispatches({"workflows": [{"workflow_id": "other", "status": "Running"}]})
    assert [row["workflow_id"] for row in summary["workflows"]] == [WF, "other"]
    assert summary["pending_count"] == 1 and summary["workflows"][0]["status"] == "Submitted"

    machine = app_module.list_admin_actions("machine-a")
    assert WF in machine["confirmed_running_workflow_ids"]
    assert machine["live_workflow_statuses"][WF] == "Submitted"


def test_local_argo_backend_gets_no_pending_rows(monkeypatch):
    from backfill_dashboard import app as app_module

    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info({"MLOPS_DASHBOARD_RUNTIME": "local"}))
    monkeypatch.setattr(app_module.admin_repository, "list", lambda: pytest.fail("not consulted locally"))
    summary = {"workflows": []}
    assert app_module._with_pending_dispatches(summary) is summary and summary == {"workflows": []}


def test_live_ops_ui_renders_pending_rows_without_a_stop_button_before_a_workflow_id():
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    table = (src / "features" / "workflows" / "WorkflowTable.tsx").read_text(encoding="utf-8")
    assert "pending-row" in table and "workflow.pending_detail" in table and "github_run_url" in table
    ops = (src / "pages" / "admin" / "OperationsPage.tsx").read_text(encoding="utf-8")
    assert "w.pending && w.workflow_id.startsWith('pending-') ? <span" in ops


def test_ui_fetches_status_only_on_refresh_or_push():
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    hook = (src / "features" / "workflows" / "useWorkflowSummary.ts").read_text(encoding="utf-8")
    assert "setInterval" not in hook and "postWorkflowRefresh()" in hook and "useWorkflowLiveSocket" in hook
    for page in ("pages/monitor/LivePage.tsx", "pages/monitor/MachinePage.tsx", "pages/admin/OperationsPage.tsx"):
        text = (src / page).read_text(encoding="utf-8")
        assert "setInterval" not in text, page
        assert "retryIn={summary.retryIn}" in text and "fetching={summary.fetching}" in text, page
    button = (src / "components" / "ui" / "index.tsx").read_text(encoding="utf-8")
    assert "disabled={fetching || retryIn > 0}" in button and "`Retry in ${retryIn}s`" in button and "'Refreshing…'" in button
    helper = (src / "features" / "workflows" / "workflowStatusRefresh.ts").read_text(encoding="utf-8")
    assert "/api/admin/workflows/refresh" in helper and "method: 'POST'" in helper

