"""GitHub Actions dispatch backend - every GitHub call goes to a loopback fake.

Nothing here reaches api.github.com or Outerbounds: the client is pointed at a
threaded fake GitHub API on 127.0.0.1 (GITHUB_DISPATCH_API_URL), including the
real admin-action child process (``python -m backfill_dashboard.github_dispatch``).
"""
from __future__ import annotations

import base64
import io
import json
import re
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from backfill_dashboard import github_dispatch as gha
from backfill_dashboard import outerbounds_gha
from backfill_dashboard.admin import (
    AdminActionRepository,
    LogLookupParams,
    PROD_CONFIRMATION,
    TerminateParams,
    TriggerParams,
    WorkflowCommandBuilder,
    WorkflowSourceRequest,
)
from backfill_dashboard.outerbounds_actions import CampaignDispatchNotSupported, OperationNotSupported, WorkflowBackendError
from backfill_dashboard.outerbounds_gha import (
    GitHubDispatchNotConfiguredError,
    GithubActionsCampaignAdapter,
    GithubActionsCommandBuilder,
    GithubActionsRunningWorkflowProvider,
)
from backfill_dashboard.runtime_mode import runtime_info

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / ".github" / "workflows" / "outerbounds-dispatch.yml"
TOKEN = "github_pat_FAKE0123456789abcdefFAKE0123456789"
REPO = "octo-test/dash"
MACHINE = "683ec59079fecb5a5a240478"
OTHER_MACHINE = "6817571193e37ef05fffcd1c"
DEPLOYMENTS = {
    "OUTERBOUNDS_FSTBACKFILL_DEPLOYMENT_ID": "fstbackfill.test.fstbackfill-abc",
    "OUTERBOUNDS_ULRPM_ORCHESTRATOR_DEPLOYMENT_ID": "fstbkfill.test.ulrpmorchestrator-abc",
}


# --- fake GitHub API -------------------------------------------------------------------------

class FakeGitHub:
    """In-memory GitHub Actions: dispatch creates a run; each GET of the run advances it."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.artifacts: dict[int, bytes] = {}
        self.dispatches: list[dict] = []
        self.auth_headers: list[str | None] = []
        self.blob_auth: list[str | None] = []
        self.polls_to_complete = 1
        self.result_for: callable = lambda inputs: {"ok": True, "request_id": inputs["request_id"], "action": inputs["action"]}
        self.fail_status: int | None = None
        self.hide_runs = False
        self.upload_artifact = True
        self.lock = threading.Lock()
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add_run(self, inputs: dict, *, status: str = "queued", conclusion: str | None = None) -> dict:
        with self.lock:
            run_id = 1000 + len(self.runs)
            run = {"id": run_id, "name": "Outerbounds dispatch", "display_title": f"ob-{inputs['action']}-{inputs['request_id']}",
                   "status": status, "conclusion": conclusion, "created_at": f"2026-10-11T12:00:{len(self.runs):02d}Z",
                   "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}", "_inputs": inputs, "_polls": 0}
            self.runs.append(run)
            if status == "completed":
                self._finish(run)
            return run

    def _finish(self, run: dict) -> None:
        run["status"] = "completed"
        result = self.result_for(run["_inputs"])
        run["conclusion"] = run["conclusion"] or ("success" if result.get("ok") else "failure")
        if self.upload_artifact:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("result.json", json.dumps(result))
            self.artifacts[run["id"]] = buffer.getvalue()

    def _public(self, run: dict) -> dict:
        return {key: value for key, value in run.items() if not key.startswith("_")}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _send(self, status: int, payload=None, headers: dict | None = None, raw: bytes | None = None):
                body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                fake.auth_headers.append(self.headers.get("Authorization"))
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if fake.fail_status:
                    return self._send(fake.fail_status, {"message": f"Bad credentials {TOKEN}"})
                if self.path.endswith("/dispatches"):
                    fake.dispatches.append(body)
                    if not fake.hide_runs:
                        fake.add_run(body["inputs"])
                    return self._send(204)
                self._send(404, {"message": "Not Found"})

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                path, query = parsed.path, parse_qs(parsed.query)
                if path.startswith("/blob/"):
                    fake.blob_auth.append(self.headers.get("Authorization"))
                    return self._send(200, raw=fake.artifacts[int(path.rsplit("/", 1)[-1])])
                fake.auth_headers.append(self.headers.get("Authorization"))
                if fake.fail_status:
                    return self._send(fake.fail_status, {"message": "Bad credentials"})
                base = f"/repos/{REPO}"
                if path == base:
                    return self._send(200, {"full_name": REPO, "private": False})
                if path == f"{base}/actions/workflows/outerbounds-dispatch.yml":
                    return self._send(200, {"state": "active", "path": ".github/workflows/outerbounds-dispatch.yml"})
                if path == f"{base}/contents/.github/workflows/outerbounds-dispatch.yml":
                    return self._send(200, {"content": base64.b64encode(WORKFLOW.read_bytes()).decode()})
                if path == f"{base}/actions/workflows/outerbounds-dispatch.yml/runs":
                    assert query["event"] == ["workflow_dispatch"] and query["created"][0].startswith(">=")
                    return self._send(200, {"workflow_runs": [fake._public(run) for run in fake.runs]})
                match = re.fullmatch(rf"{base}/actions/runs/(\d+)(/artifacts)?", path)
                if match:
                    run = next(item for item in fake.runs if item["id"] == int(match.group(1)))
                    if match.group(2):
                        artifacts = ([{"id": run["id"], "name": f"ob-result-{run['_inputs']['request_id']}", "expired": False}]
                                     if run["id"] in fake.artifacts else [])
                        assert query["name"] == [f"ob-result-{run['_inputs']['request_id']}"]
                        return self._send(200, {"artifacts": artifacts})
                    with fake.lock:
                        run["_polls"] += 1
                        if run["status"] != "completed" and run["_polls"] >= fake.polls_to_complete:
                            fake._finish(run)
                    return self._send(200, fake._public(run))
                match = re.fullmatch(rf"{base}/actions/artifacts/(\d+)/zip", path)
                if match:
                    return self._send(302, headers={"Location": f"{fake.url}/blob/{match.group(1)}"})
                self._send(404, {"message": "Not Found"})

        return Handler


@pytest.fixture()
def github():
    fake = FakeGitHub()
    yield fake
    fake.close()


def _env(github: FakeGitHub, **extra) -> dict:
    return {"GITHUB_DISPATCH_TOKEN": TOKEN, "GITHUB_DISPATCH_REPO": REPO, "GITHUB_DISPATCH_REF": "main",
            "GITHUB_DISPATCH_WORKFLOW": "outerbounds-dispatch.yml", "GITHUB_DISPATCH_API_URL": github.url,
            "GITHUB_DISPATCH_POLL_SECONDS": "1", **extra}


def _client(github: FakeGitHub) -> gha.GitHubClient:
    return gha.GitHubClient(gha.DispatchConfig.from_env(_env(github)))


# --- client: dispatch, lookup, polling, artifact ------------------------------------------------

def test_dispatch_finds_run_by_request_id_polls_and_unzips_result(github):
    github.polls_to_complete = 3
    github.result_for = lambda inputs: {"ok": True, "request_id": inputs["request_id"], "action": "trigger",
                                        "run_id": "argo-fstbackfill-x7k2p", "workflow_id": "fstbackfill-x7k2p"}
    inputs = gha.build_inputs("trigger", "trigger-0123456789abcdef", flow="FSTBackfill",
                              deployment_id="fstbackfill.test.fstbackfill-abc", params={"name-space": "dev-ns"})
    result = gha.run_action("trigger", "trigger-0123456789abcdef", inputs, client=_client(github), sleep=lambda _s: None)
    assert result["ok"] and result["workflow_id"] == "fstbackfill-x7k2p"
    assert result["github_run_url"].endswith("/actions/runs/1000") and result["github_conclusion"] == "success"
    assert len(github.dispatches) == 1
    dispatched = github.dispatches[0]
    assert dispatched["ref"] == "main"
    assert dispatched["inputs"] == {"action": "trigger", "flow": "FSTBackfill", "deployment_id": "fstbackfill.test.fstbackfill-abc",
                                    "params_json": '{"name-space":"dev-ns"}', "run_id": "", "request_id": "trigger-0123456789abcdef"}
    assert all(header == f"Bearer {TOKEN}" for header in github.auth_headers)
    # The signed artifact URL never receives the GitHub token.
    assert github.blob_auth == [None]


def test_existing_run_for_request_id_is_reused_without_a_second_dispatch(github):
    inputs = gha.build_inputs("list_runs", "listruns-abcdef012345", params={"limit": 3})
    github.add_run(inputs, status="completed")
    result = gha.run_action("list_runs", "listruns-abcdef012345", inputs, client=_client(github), sleep=lambda _s: None)
    assert result["ok"] and github.dispatches == []
    # A retry of a run that is still in progress waits for it instead of dispatching.
    other = gha.build_inputs("list_runs", "listruns-inprogress01", params={"limit": 3})
    github.add_run(other, status="in_progress")
    assert gha.run_action("list_runs", "listruns-inprogress01", other, client=_client(github), sleep=lambda _s: None)["ok"]
    assert github.dispatches == []


def test_run_that_never_appears_or_never_finishes_times_out_clearly(github):
    github.hide_runs = True
    ticks = iter(range(0, 10_000, 30))
    inputs = gha.build_inputs("status", "status-000000000001", run_id="argo-abc-123")
    with pytest.raises(gha.GitHubDispatchTimeout, match="no matching run appeared") as missing:
        gha.run_action("status", "status-000000000001", inputs, client=_client(github), sleep=lambda _s: None,
                       clock=lambda: next(ticks))
    assert missing.value.status_code == 504 and len(github.dispatches) == 1

    github.hide_runs = False
    github.polls_to_complete = 10_000
    ticks = iter(range(0, 100_000, 100))
    inputs = gha.build_inputs("status", "status-000000000002", run_id="argo-abc-123")
    with pytest.raises(gha.GitHubDispatchTimeout, match="did not finish within") as slow:
        gha.run_action("status", "status-000000000002", inputs, client=_client(github), sleep=lambda _s: None,
                       clock=lambda: next(ticks), timeout=120)
    assert slow.value.run_url and "/actions/runs/" in slow.value.run_url


def test_missing_artifact_and_http_errors_are_clear_and_never_leak_the_token(github):
    github.upload_artifact = False
    inputs = gha.build_inputs("list_runs", "listruns-noartifact1")
    with pytest.raises(gha.GitHubDispatchError, match="has no artifact ob-result-listruns-noartifact1") as missing:
        gha.run_action("list_runs", "listruns-noartifact1", inputs, client=_client(github), sleep=lambda _s: None)
    assert missing.value.error_type == "github_result_missing"

    github.fail_status = 401
    with pytest.raises(gha.GitHubDispatchError) as denied:
        _client(github).dispatch(inputs)
    text = str(denied.value)
    assert denied.value.error_type == "github_http_401" and "invalid or expired" in text
    assert TOKEN not in text and "<redacted" in text
    github.fail_status = 404
    with pytest.raises(gha.GitHubDispatchError, match="Actions read/write and Contents read"):
        _client(github).get_workflow()


def test_oversized_inputs_are_refused_before_dispatch(github):
    inputs = gha.build_inputs("trigger", "trigger-toolarge0001", flow="UlrpmDevBackfillOrchestratorFlow",
                              deployment_id="x", params={"machine_manifest_plan": "x" * 70_000})
    with pytest.raises(gha.GitHubDispatchError, match="too large") as error:
        _client(github).dispatch(inputs)
    assert error.value.status_code == 422 and github.dispatches == []


def test_config_validation_and_request_id_scheme():
    with pytest.raises(gha.GitHubDispatchNotConfigured, match="GITHUB_DISPATCH_TOKEN") as missing:
        gha.DispatchConfig.from_env({})
    assert missing.value.status_code == 503 and missing.value.error_type == "github_dispatch_not_configured"
    for key, value in (("GITHUB_DISPATCH_REPO", "evil repo"), ("GITHUB_DISPATCH_REF", "main;rm"),
                       ("GITHUB_DISPATCH_WORKFLOW", "../x.yml"), ("GITHUB_DISPATCH_API_URL", "http://evil.example")):
        with pytest.raises(gha.GitHubDispatchNotConfigured):
            gha.DispatchConfig.from_env({"GITHUB_DISPATCH_TOKEN": TOKEN, key: value})
    payload = {"flow": "FSTBackfill", "params": {"machine_ids": MACHINE, "start_index": 17}}
    first = gha.request_id_for("trigger", payload, "action-1")
    assert first == gha.request_id_for("trigger", dict(reversed(list(payload.items()))), "action-1")  # deterministic
    assert first != gha.request_id_for("trigger", payload, "action-2")  # another pending action
    assert first != gha.request_id_for("trigger", {**payload, "params": {"machine_ids": MACHINE, "start_index": 18}}, "action-1")
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", first) and first.startswith("trigger-")
    with pytest.raises(ValueError):
        gha.build_inputs("trigger", "bad id; rm -rf /")


# --- runtime selection / missing token ----------------------------------------------------------

GHA_RUNTIME = {"MLOPS_DASHBOARD_RUNTIME": "databricks", "DATABRICKS_APP_PORT": "8000"}


def test_databricks_without_token_returns_503_github_dispatch_not_configured(monkeypatch):
    from test_runtime_mode import _request

    from backfill_dashboard import app as app_module

    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info(GHA_RUNTIME))
    monkeypatch.setattr(app_module.admin_repository, "create", lambda *a, **k: pytest.fail("side effect before token check"))
    for path in ("/api/admin/workflows/trigger", "/api/admin/backfills/orchestrated", "/api/admin/workflows/terminate",
                 "/api/v1/backfill-campaigns"):
        response = _request(app_module.app, "POST", path, {})
        assert response.status_code == 503, (path, response.body)
        assert response.body["error_type"] == "github_dispatch_not_configured"
        assert "GITHUB_DISPATCH_TOKEN" in response.body["detail"] and "METAFLOW_SERVICE_AUTH_KEY" in response.body["detail"]
    spec = _request(app_module.app, "GET", "/api/admin/spec").body
    assert spec["runtime"]["workflow_backend"] == "github_actions"


def test_builder_and_provider_without_token_explain_instead_of_failing():
    builder = GithubActionsCommandBuilder(environ=dict(DEPLOYMENTS))
    with pytest.raises(GitHubDispatchNotConfiguredError, match="GITHUB_DISPATCH_TOKEN") as error:
        builder.build_trigger(WorkflowSourceRequest(), TriggerParams(storage_account_manifest_path="m/a.parquet"))
    assert error.value.status_code == 503
    readiness = builder.platform_readiness()
    assert readiness == {**readiness, "ready": False, "error_type": "github_dispatch_not_configured", "backend": "github_actions"}
    provider = GithubActionsRunningWorkflowProvider(environ={}, runner=lambda *a, **k: pytest.fail("no dispatch without token"))
    assert provider.list_recent()["argo_available"] is False
    assert provider.lookup_workflows(["abc-123"]) == []


def test_factory_wires_github_actions_backend_on_databricks_without_key(monkeypatch, tmp_path):
    from dataclasses import replace

    from backfill_dashboard import factory
    from backfill_dashboard.admin import RunningWorkflowProvider
    from backfill_dashboard.config import get_settings

    settings = replace(get_settings(), control_plane_db_path=tmp_path / "cp.sqlite3",
                       state_path=tmp_path / "state.json", dispatch_enabled=True)
    monkeypatch.setattr(factory, "get_settings", lambda: settings)
    monkeypatch.setenv("BACKFILL_CONTROL_STORE", "local_json")
    monkeypatch.setattr(factory, "build_lifecycle_provider", lambda: (None, "off"))
    monkeypatch.setattr(factory, "log_startup_data_sources", lambda *a, **k: None)
    for key in ("METAFLOW_SERVICE_AUTH_KEY", "OBP_METAFLOW_CONFIG_URL", "OUTERBOUNDS_DISPATCH_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", TOKEN)

    monkeypatch.setenv("MLOPS_DASHBOARD_RUNTIME", "local")
    local = factory.build_components()
    assert type(local[5]) is WorkflowCommandBuilder  # local regression: argo CLI path unchanged
    assert type(local[7]) is RunningWorkflowProvider

    monkeypatch.setenv("MLOPS_DASHBOARD_RUNTIME", "databricks")
    hosted = factory.build_components()
    assert isinstance(hosted[5], GithubActionsCommandBuilder)
    assert isinstance(hosted[7], GithubActionsRunningWorkflowProvider)
    assert isinstance(hosted[10].runtime.dispatcher.adapter, GithubActionsCampaignAdapter)


# --- builder: same parameters as the argo path, child commands ---------------------------------

def _builder(**extra) -> GithubActionsCommandBuilder:
    return GithubActionsCommandBuilder(environ={"GITHUB_DISPATCH_TOKEN": TOKEN, **DEPLOYMENTS, **extra})


def test_trigger_commands_carry_the_argo_parameters_and_keep_lanes_sequential():
    plan = {MACHINE: [{"month_index": 17, "manifest": "p/a.parquet", "since": "2026/01/01/00", "until": "2026/01/08/00"}]}
    kwargs = dict(machine_ids=[MACHINE, OTHER_MACHINE], manifest_template="p/m_{month_index}.parquet", start_index=17,
                  end_index=18, params=TriggerParams(max_parallel_steps=2),
                  month_indices_by_machine={MACHINE: [17], OTHER_MACHINE: [17]}, manifest_plan=plan)
    cwd, command = _builder().build_ulrpm_orchestrator_trigger(**kwargs)
    assert command[1:3] == ["-m", "backfill_dashboard.github_dispatch"] and Path(command[0]).is_absolute()
    request = json.loads(command[-1])
    assert request["op"] == "trigger"
    inputs = request["inputs"]
    assert inputs["flow"] == "UlrpmDevBackfillOrchestratorFlow"
    assert inputs["deployment_id"] == DEPLOYMENTS["OUTERBOUNDS_ULRPM_ORCHESTRATOR_DEPLOYMENT_ID"]
    assert inputs["params"]["child_max_parallel_steps"] == 1  # same-machine chunks strictly sequential
    assert json.loads(inputs["params"]["machine_manifest_plan"]) == plan
    _, argo = WorkflowCommandBuilder().build_ulrpm_orchestrator_trigger(**kwargs)
    argo_values = dict(item.split("=", 1) for item in argo if "=" in item)
    assert all(json.dumps(inputs["params"][name]) == argo_values[name] for name in inputs["params"])
    assert TOKEN not in " ".join(command)
    # FSTBackfill validation is shared with the argo path.
    with pytest.raises(ValueError, match=PROD_CONFIRMATION):
        _builder().build_trigger(WorkflowSourceRequest(), TriggerParams(environment="prod", storage_account_manifest_path="m"))


def test_terminate_logs_create_and_payload_limits():
    _, command = _builder().build_terminate(TerminateParams(workflow_id="argo-ulrpm-live"))
    inputs = json.loads(command[-1])["inputs"]
    assert inputs["run_id"] == "argo-ulrpm-live"
    assert set(inputs["deployment_id"].split(",")) == set(DEPLOYMENTS.values())
    with pytest.raises(OperationNotSupported, match="local-only"):
        _builder().build_terminate(TerminateParams(workflow_id=""))
    with pytest.raises(OperationNotSupported, match="Outerbounds UI") as logs:
        _builder().build_logs(WorkflowSourceRequest(), LogLookupParams(run_task_path="argo-a/start/1"))
    assert logs.value.status_code == 501
    with pytest.raises(OperationNotSupported):
        _builder().build_create(WorkflowSourceRequest())
    huge = TriggerParams(storage_account_manifest_path="m/" + "a" * 70_000)
    with pytest.raises(WorkflowBackendError, match="too large") as too_large:
        _builder().build_trigger(WorkflowSourceRequest(), huge)
    assert too_large.value.status_code == 422


def test_campaign_dispatch_stays_refused():
    adapter = GithubActionsCampaignAdapter()
    with pytest.raises(CampaignDispatchNotSupported, match="not supported through GitHub Actions"):
        adapter.submit({"key": "k"})
    assert adapter.find({"key": "k"}) is None


# --- admin action child: real process against the fake GitHub ---------------------------------

def _child_env(github: FakeGitHub, monkeypatch) -> None:
    for key, value in _env(github).items():
        monkeypatch.setenv(key, value)


def test_trigger_child_records_workflow_id_and_salts_request_id_with_action_id(github, monkeypatch, tmp_path):
    _child_env(github, monkeypatch)
    github.result_for = lambda inputs: {"ok": True, "request_id": inputs["request_id"], "action": "trigger",
                                        "run_id": "argo-fstbackfill-q1w2e", "workflow_id": "fstbackfill-q1w2e"}
    cwd, command = _builder().build_trigger(WorkflowSourceRequest(), TriggerParams(storage_account_manifest_path="m/a.parquet"))
    repository = AdminActionRepository(tmp_path / "actions.json")
    action = repository.create(action="trigger", command=command, cwd=cwd)
    repository.run(action.id)
    stored = repository.get(action.id)
    assert stored["status"] == "running" and stored["phase"] == "accepted", stored
    assert stored["workflow_id"] == "fstbackfill-q1w2e"
    assert TOKEN not in json.dumps(stored)
    request_id = github.dispatches[0]["inputs"]["request_id"]
    payload = json.loads(command[-1])["inputs"]
    assert request_id == gha.request_id_for("trigger", payload, action.id)  # pending action id = idempotency salt

    # The same pending action re-run (e.g. a retried child) reuses the GitHub run: no second dispatch.
    assert gha.child_main({"op": "trigger", "inputs": payload, "salt": action.id}, environ={**_env(github)})["ok"]
    assert len(github.dispatches) == 1


def test_failed_action_result_surfaces_as_action_error(github, monkeypatch, tmp_path):
    _child_env(github, monkeypatch)
    github.result_for = lambda inputs: {"ok": False, "request_id": inputs["request_id"], "action": "terminate",
                                        "error_type": "LookupError", "error": "Could not find workflow abc on Argo Workflows"}
    cwd, command = _builder().build_terminate(TerminateParams(workflow_id="abc-123"))
    repository = AdminActionRepository(tmp_path / "actions.json")
    action = repository.create(action="terminate", command=command, cwd=cwd)
    repository.run(action.id)
    stored = repository.get(action.id)
    assert stored["status"] == "failed" and "Could not find workflow abc" in stored["error"]


def test_child_reports_missing_token_without_dispatching(github, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    code = gha.main([json.dumps({"op": "list_runs", "inputs": {}})])
    out = capsys.readouterr().out
    assert code == 1 and gha.ACTION_MARKER in out and "github_dispatch_not_configured" in out
    assert github.dispatches == []


# --- duplicate submit / sequential guards on the GitHub Actions path ---------------------------

def test_orchestrated_submit_blocks_duplicates_on_the_github_path(github, monkeypatch, tmp_path):
    from fastapi import HTTPException
    from test_outerbounds_actions import _queued_orchestrated, _run_background

    _child_env(github, monkeypatch)
    github.result_for = lambda inputs: {"ok": True, "request_id": inputs["request_id"], "action": "trigger",
                                        "run_id": "argo-ulrpmorchestrator-z9y8x", "workflow_id": "ulrpmorchestrator-z9y8x"}
    ctx = _queued_orchestrated(monkeypatch, tmp_path, _builder())
    import backfill_dashboard.outerbounds_gha as module  # noqa: F401 - contract check runs in the workflow

    monkeypatch.setattr(ctx.app, "_require_split_supported", lambda split: None)
    first, background = ctx.submit(MACHINE)
    with pytest.raises(HTTPException) as duplicate:
        ctx.submit(OTHER_MACHINE)
    assert duplicate.value.status_code == 409
    _run_background(background)
    action = ctx.repository.get(first["action"]["id"])
    assert action["phase"] == "accepted", action.get("error")
    assert ctx.control.get_month_state(MACHINE, ctx.month)["parent_workflow_id"] == "ulrpmorchestrator-z9y8x"
    assert len(github.dispatches) == 1
    assert json.loads(github.dispatches[0]["inputs"]["params_json"])["child_max_parallel_steps"] == 1

    second, background = ctx.submit(MACHINE)
    _run_background(background)
    rejected = ctx.repository.get(second["action"]["id"])
    assert rejected["status"] == "failed" and "already has an active backfill" in rejected["error"]
    assert len(github.dispatches) == 1


def test_admin_run_passes_the_pending_action_id_to_the_child(monkeypatch, tmp_path):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs.get("env") or {})
        return SimpleNamespace(returncode=0, stdout="Name: wf-1\n", stderr="")

    monkeypatch.setattr("backfill_dashboard.admin.subprocess.run", fake_run)
    repository = AdminActionRepository(tmp_path / "actions.json")
    action = repository.create(action="trigger", command=["true"], cwd=tmp_path)
    repository.run(action.id)
    assert seen["DASHBOARD_ADMIN_ACTION_ID"] == action.id


# --- status provider: cached, background refresh ---------------------------------------------

def test_status_provider_never_refreshes_by_itself_and_serves_snapshots():
    calls, pending = [], []

    def runner(action, request_id, inputs, **kwargs):
        calls.append((action, request_id, inputs))
        return {"ok": True, "github_run_url": "https://github.com/x/actions/runs/1", "workflows": [
            {"metadata": {"name": "fstbackfill-aaa", "annotations": {"metaflow/flow_name": "FSTBackfill"}},
             "status": {"phase": "Running"}},
            {"metadata": {"name": "fstbackfill-bbb", "annotations": {"metaflow/flow_name": "FSTBackfill"}},
             "status": {"phase": "Succeeded"}}]}

    now = [0.0]
    provider = GithubActionsRunningWorkflowProvider(environ={"GITHUB_DISPATCH_TOKEN": TOKEN}, runner=runner,
                                                    start_thread=pending.append, clock=lambda: now[0])
    first = provider.list_running()
    assert first["workflows"] == [] and first["refreshing"] is False and calls == [] and pending == []
    assert any("click Refresh" in w for w in first["warnings"])
    assert provider.lookup_workflows(["fstbackfill-aaa"]) == [] and pending == []  # reads never dispatch
    assert provider.start_refresh() is True and provider.refreshing is True
    assert provider.start_refresh() is False and len(pending) == 1  # single flight: callers join
    pending.pop()()
    assert provider.refreshing is False and provider.snapshot_version == 1 and provider.last_completed() == 0.0
    running = provider.list_running()
    assert [item["workflow_id"] for item in running["workflows"]] == ["fstbackfill-aaa"]
    recent = provider.list_recent()
    assert recent["counts"] == {"active": 1, "completed": 1, "failed": 0} and recent["snapshot_version"] == 1
    assert recent["workflow_backend"] == "github_actions" and any("GitHub Actions" in w for w in recent["warnings"])
    assert [item["workflow_id"] for item in provider.lookup_workflows(["argo-fstbackfill-bbb"])] == ["fstbackfill-bbb"]
    action, request_id, inputs = calls[0]
    assert action == "list_runs" and inputs["action"] == "list_runs" and inputs["request_id"] == request_id
    now[0] = 10_000.0
    provider.list_running()
    provider.list_recent()
    assert pending == [] and len(calls) == 1  # no TTL refresh anymore


def test_status_refresh_error_keeps_last_good_list():
    results = iter([{"ok": True, "workflows": [{"metadata": {"name": "wf-a"}, "status": {"phase": "Running"}}]},
                    gha.GitHubDispatchTimeout("slow runner")])

    def runner(*args, **kwargs):
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    provider = GithubActionsRunningWorkflowProvider(environ={"GITHUB_DISPATCH_TOKEN": TOKEN}, runner=runner,
                                                    start_thread=lambda target: target())
    provider.start_refresh()
    provider.start_refresh()
    payload = provider.list_running()
    assert [item["workflow_id"] for item in payload["workflows"]] == ["wf-a"]
    assert "slow runner" in payload["error"] and payload["error_type"] == "github_dispatch_timeout"


# --- probe stage ------------------------------------------------------------------------------

def _stage(monkeypatch, ctx=None):
    from backfill_dashboard import outerbounds_probe as probe

    return probe.stage_github_dispatch(probe.ProbeConfig.from_env(), {"flow": "FSTBackfill", "probe_id": "abc123def456", **(ctx or {})})


def test_probe_stage_skips_or_fails_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.setenv("MLOPS_DASHBOARD_RUNTIME", "local")
    monkeypatch.delenv("OUTERBOUNDS_DISPATCH_BACKEND", raising=False)
    assert _stage(monkeypatch)["status"] == "skip"
    monkeypatch.setenv("MLOPS_DASHBOARD_RUNTIME", "databricks")
    monkeypatch.delenv("METAFLOW_SERVICE_AUTH_KEY", raising=False)
    stage = _stage(monkeypatch)
    assert stage["status"] == "fail" and stage["error_type"] == "github_dispatch_not_configured"


def test_probe_stage_read_only_checks_and_gated_dispatch(github, monkeypatch):
    _child_env(github, monkeypatch)
    monkeypatch.delenv("OUTERBOUNDS_PROBE_GHA_DISPATCH", raising=False)
    stage = _stage(monkeypatch, {"include_gha_dispatch": True})
    assert stage["status"] == "pass", stage
    assert "PUBLIC" in stage["detail"] and "end-to-end dispatch not run" in stage["detail"]
    assert github.dispatches == []  # read-only by default, even when the request asks
    assert TOKEN not in json.dumps(stage)

    monkeypatch.setenv("OUTERBOUNDS_PROBE_GHA_DISPATCH", "1")
    github.result_for = lambda inputs: {"ok": True, "request_id": inputs["request_id"], "action": "list_runs",
                                        "workflows": [{"metadata": {"name": "a"}, "status": {"phase": "Succeeded"}}]}
    stage = _stage(monkeypatch, {"include_gha_dispatch": True})
    assert stage["status"] == "pass" and "Outerbounds login OK, 1 run(s) listed" in stage["detail"], stage
    inputs = github.dispatches[0]["inputs"]
    assert inputs["action"] == "list_runs" and inputs["flow"] == "FSTBackfill" and inputs["request_id"] == "probe-abc123def456"

    github.fail_status = 403
    stage = _stage(monkeypatch)
    assert stage["status"] == "fail" and stage["error_type"] == "github_http_403"


def test_probe_api_accepts_include_gha_dispatch_and_status_reports_settings(monkeypatch):
    from test_runtime_mode import _request

    from backfill_dashboard import app as app_module
    from backfill_dashboard import outerbounds_probe as probe

    monkeypatch.setenv("OUTERBOUNDS_PROBE_ENABLED", "1")
    seen = {}
    monkeypatch.setattr(probe, "run_probe", lambda flow, trig, dep, gha_dispatch=False: seen.update(gha=gha_dispatch) or {"stages": []})
    assert _request(app_module.app, "POST", "/api/diagnostics/outerbounds", {"include_gha_dispatch": True}).status_code == 200
    assert seen["gha"] is True
    assert _request(app_module.app, "POST", "/api/diagnostics/outerbounds", {"include_gha_dispatch": "yes"}).status_code == 422
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", TOKEN)
    status = _request(app_module.app, "GET", "/api/diagnostics/outerbounds/status").body
    assert set(status["github_dispatch"]) == {"token_configured", "repo", "ref", "workflow", "dispatch_enabled"}
    assert status["github_dispatch"]["token_configured"] is True
    assert TOKEN not in json.dumps(status)  # key names only, never the value


# --- scripts/ob_dispatch.py: strict input validation -------------------------------------------

sys.path.insert(0, str(SCRIPTS))
import ob_dispatch  # noqa: E402


def _inputs(**overrides) -> dict:
    base = {"OB_ACTION": "trigger", "OB_FLOW": "FSTBackfill", "OB_DEPLOYMENT_ID": "fstbackfill.test.fstbackfill-abc",
            "OB_PARAMS_JSON": json.dumps({"name-space": "ulrpm-fst-dev-20260830", "storage_account_manifest_path": "m/a.parquet",
                                          "manifest_bucket_count": 3, "force_sessions_from_bucket": False,
                                          "features_to_backfill": ""}),
            "OB_RUN_ID": "", "OB_REQUEST_ID": "trigger-0123456789abcdef"}
    return {**base, **overrides}


def test_script_accepts_valid_requests():
    request = ob_dispatch.validate(_inputs())
    assert request["params"]["manifest_bucket_count"] == 3 and request["deployment_ids"] == ["fstbackfill.test.fstbackfill-abc"]
    status = ob_dispatch.validate(_inputs(OB_ACTION="status", OB_RUN_ID="argo-fstbackfill-x7k2p", OB_PARAMS_JSON="", OB_DEPLOYMENT_ID=""))
    assert status["workflow_name"] == "fstbackfill-x7k2p"
    listing = ob_dispatch.validate(_inputs(OB_ACTION="list_runs", OB_FLOW="", OB_DEPLOYMENT_ID="", OB_PARAMS_JSON='{"limit": 5}'))
    assert listing["limit"] == 5 and listing["flow"] is None
    terminate = ob_dispatch.validate(_inputs(OB_ACTION="terminate", OB_RUN_ID="abc-123", OB_PARAMS_JSON="",
                                             OB_DEPLOYMENT_ID="a.b,c.d"))
    assert terminate["deployment_ids"] == ["a.b", "c.d"]


@pytest.mark.parametrize("overrides,message", [
    ({"OB_REQUEST_ID": "x; rm -rf /"}, "request_id"),
    ({"OB_REQUEST_ID": "$(curl evil)"}, "request_id"),
    ({"OB_REQUEST_ID": "short"}, "request_id"),
    ({"OB_REQUEST_ID": "a" * 65}, "request_id"),
    ({"OB_ACTION": "delete"}, "action must be one of"),
    ({"OB_ACTION": "trigger\nterminate"}, "action must be one of"),
    ({"OB_FLOW": "EvilFlow"}, "flow must be one of"),
    ({"OB_FLOW": "FSTBackfill; echo"}, "flow must be one of"),
    ({"OB_DEPLOYMENT_ID": "x`id`"}, "deployment_id has an invalid format"),
    ({"OB_DEPLOYMENT_ID": "a,b"}, "exactly one deployment_id"),
    ({"OB_DEPLOYMENT_ID": ""}, "deployment_id is required"),
    ({"OB_PARAMS_JSON": "[1,2]"}, "must be a JSON object"),
    ({"OB_PARAMS_JSON": "{bad json"}, "not valid JSON"),
    ({"OB_PARAMS_JSON": '{"evil_param": "x"}'}, "unsupported keys: evil_param"),
    ({"OB_PARAMS_JSON": '{"manifest_bucket_count": "3"}'}, "must be an integer"),
    ({"OB_PARAMS_JSON": '{"manifest_bucket_count": true}'}, "must be an integer"),
    ({"OB_PARAMS_JSON": '{"force_sessions_from_bucket": "false"}'}, "must be a boolean"),
    ({"OB_PARAMS_JSON": '{"name-space": "a\\nb"}'}, "control characters"),
    ({"OB_PARAMS_JSON": '{"name-space": "feature-store-container"}'}, "production namespace"),
    ({"OB_PARAMS_JSON": "x" * 70_000}, "too large"),
    ({"OB_RUN_ID": "argo-abc"}, "takes no run_id"),
    ({"OB_ACTION": "status", "OB_RUN_ID": "../etc/passwd", "OB_PARAMS_JSON": ""}, "run_id must look like"),
    ({"OB_ACTION": "status", "OB_RUN_ID": "argo-abc-123", "OB_PARAMS_JSON": '{"limit": 1}'}, "takes no params_json"),
    ({"OB_ACTION": "list_runs", "OB_PARAMS_JSON": '{"limit": 1000}', "OB_DEPLOYMENT_ID": ""}, "limit must be between"),
])
def test_script_rejects_injection_and_out_of_allowlist_inputs(overrides, message):
    with pytest.raises(ob_dispatch.InputError, match=re.escape(message)):
        ob_dispatch.validate(_inputs(**overrides))


def test_script_allowlists_deployments_and_gates_prod_by_repository_variable():
    with pytest.raises(ob_dispatch.InputError, match="not in the OB_DISPATCH_DEPLOYMENTS allowlist"):
        ob_dispatch.validate(_inputs(OB_DISPATCH_DEPLOYMENTS="other.deployment"))
    assert ob_dispatch.validate(_inputs(OB_DISPATCH_DEPLOYMENTS="fstbackfill.test.fstbackfill-abc,x"))
    prod = _inputs(OB_PARAMS_JSON='{"name-space": "feature-store-container"}', OB_DISPATCH_ALLOW_PROD="1")
    assert ob_dispatch.validate(prod)["params"]["name-space"] == "feature-store-container"


def test_script_accepts_the_configured_app_deployments_with_the_workflow_default_allowlist():
    manifest = yaml.safe_load((ROOT / "app.yaml").read_text(encoding="utf-8"))
    env = {item["name"]: item.get("value") for item in manifest["env"]}
    fst = env["OUTERBOUNDS_FSTBACKFILL_DEPLOYMENT_ID"]
    orchestrator = env["OUTERBOUNDS_ULRPM_ORCHESTRATOR_DEPLOYMENT_ID"]
    allow = f"{fst},{orchestrator}"
    assert ob_dispatch.validate(_inputs(OB_DEPLOYMENT_ID=fst, OB_DISPATCH_DEPLOYMENTS=allow))["deployment_ids"] == [fst]
    orch = ob_dispatch.validate(_inputs(OB_FLOW="UlrpmDevBackfillOrchestratorFlow", OB_DEPLOYMENT_ID=orchestrator,
                                        OB_PARAMS_JSON="{}", OB_DISPATCH_DEPLOYMENTS=allow))
    assert orch["deployment_ids"] == [orchestrator]
    stop = ob_dispatch.validate(_inputs(OB_ACTION="terminate", OB_DEPLOYMENT_ID=allow, OB_PARAMS_JSON="",
                                        OB_RUN_ID="argo-fstbackfill.test.devfstmon.fstbackfill-mrqqu-x7k2p",
                                        OB_DISPATCH_DEPLOYMENTS=allow))
    assert stop["deployment_ids"] == [fst, orchestrator]
    with pytest.raises(ob_dispatch.InputError, match="not in the OB_DISPATCH_DEPLOYMENTS allowlist"):
        ob_dispatch.validate(_inputs(OB_DEPLOYMENT_ID="fstbackfill.test.devcoreme.fstbackfill-zvsui",
                                     OB_DISPATCH_DEPLOYMENTS=allow))


def test_script_flow_parameter_allowlist_matches_the_dashboard_builders():
    plan = {MACHINE: [{"month_index": 17, "manifest": "p/a.parquet", "since": "2026/01/01/00", "until": "2026/01/08/00"}]}
    builders = {
        "FSTBackfill": _builder().build_trigger(WorkflowSourceRequest(), TriggerParams(storage_account_manifest_path="m/a.parquet")),
        "UlrpmDevBackfillOrchestratorFlow": _builder().build_ulrpm_orchestrator_trigger(
            machine_ids=[MACHINE], manifest_template="p/m_{month_index}.parquet", start_index=17, end_index=18,
            params=TriggerParams(max_parallel_steps=1), month_indices_by_machine={MACHINE: [17]}, manifest_plan=plan),
    }
    for flow, (_, command) in builders.items():
        inputs = json.loads(command[-1])["inputs"]
        assert inputs["flow"] == flow
        request = ob_dispatch.validate(_inputs(OB_FLOW=flow, OB_DEPLOYMENT_ID=inputs["deployment_id"],
                                               OB_PARAMS_JSON=json.dumps(inputs["params"])))
        assert request["params"] == inputs["params"]


def test_script_outputs_are_redacted_and_public_safe(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    env = _inputs(OB_RESULT_DIR=str(tmp_path / "out"), GITHUB_STEP_SUMMARY=str(summary), GITHUB_RUN_ID="42",
                  OB_PARAMS_JSON=json.dumps({"name-space": "ulrpm-fst-dev-20260830", "storage_account_manifest_path": f"m/{MACHINE}.parquet"}))
    fake_metaflow = SimpleNamespace(namespace=lambda value: None)
    monkeypatch.setitem(sys.modules, "metaflow", fake_metaflow)

    def failing(request, environ):
        raise RuntimeError(f"Bearer abc.def.ghi failed for m/{MACHINE}.parquet with x-api-key=supersecret123")

    monkeypatch.setitem(ob_dispatch.OPS, "trigger", failing)
    code, result = ob_dispatch.run(env)
    ob_dispatch.write_outputs(result, env)
    written = (tmp_path / "out" / "result.json").read_text() + summary.read_text()
    assert code == 1 and result["ok"] is False and result["github_run_id"] == "42"
    assert MACHINE not in written and "supersecret123" not in written and "abc.def.ghi" not in written
    assert "<param>" in written and "<redacted>" in written

    monkeypatch.setitem(ob_dispatch.OPS, "trigger", lambda request, environ: {"ok": True, "run_id": "argo-wf-1", "workflow_id": "wf-1",
                                                                             "flow": "FSTBackfill", "parameter_names": ["name_space"]})
    code, result = ob_dispatch.run(env)
    assert code == 0 and result["workflow_id"] == "wf-1" and result["request_id"] == env["OB_REQUEST_ID"]
    code, result = ob_dispatch.run({**env, "OB_REQUEST_ID": "bad id"})
    assert code == 2 and result["error_type"] == "invalid_input" and result["request_id"] is None


def test_script_mask_inputs_and_ensure_result(tmp_path, capsys):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"inputs": {"params_json": json.dumps({"machine_ids": f"{MACHINE},{OTHER_MACHINE}", "start_index": 1})}}))
    assert ob_dispatch.mask_inputs({"GITHUB_EVENT_PATH": str(event)}) == 0
    masks = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::add-mask::")]
    assert f"::add-mask::{MACHINE}" in masks and f"::add-mask::{OTHER_MACHINE}" in masks
    env = {"OB_RESULT_DIR": str(tmp_path / "res"), "OB_REQUEST_ID": "trigger-0123456789abcdef", "OB_ACTION": "trigger",
           "OB_JOB_STATUS": "failure"}
    assert ob_dispatch.ensure_result(env) == 0
    result = json.loads((tmp_path / "res" / "result.json").read_text())
    assert result["ok"] is False and result["error_type"] == "workflow_setup_failed" and "OIDC" in result["error"]
    (tmp_path / "res" / "result.json").write_text('{"ok": true}')
    ob_dispatch.ensure_result(env)
    assert json.loads((tmp_path / "res" / "result.json").read_text()) == {"ok": True}  # never overwrites a real result


def test_script_orchestrator_contract_check():
    def template(version):
        return {"spec": {"arguments": {"parameters": [{"name": "orchestrator_contract_version", "value": f'"{version}"'}]}}}

    ob_dispatch._check_orchestrator_contract(template(ob_dispatch.ORCHESTRATOR_CONTRACT), {"machine_manifest_plan": "{}"})
    ob_dispatch._check_orchestrator_contract(template("2026-09-17-v4"), {})
    with pytest.raises(ValueError, match="Full month only"):
        ob_dispatch._check_orchestrator_contract(template("2026-09-17-v4"), {"machine_manifest_plan": '{"a":1}'})
    with pytest.raises(ValueError, match="stale"):
        ob_dispatch._check_orchestrator_contract(template("2025-01-01-v1"), {})
    from backfill_dashboard.admin import ULRPM_ORCHESTRATOR_CONTRACT_VERSION, ULRPM_ORCHESTRATOR_SUPPORTED_CONTRACTS

    assert ob_dispatch.ORCHESTRATOR_CONTRACT == ULRPM_ORCHESTRATOR_CONTRACT_VERSION
    assert {ob_dispatch.ORCHESTRATOR_CONTRACT, *ob_dispatch.ORCHESTRATOR_LEGACY_CONTRACTS} == ULRPM_ORCHESTRATOR_SUPPORTED_CONTRACTS


# --- workflow YAML shape -------------------------------------------------------------------------

def test_dispatch_workflow_shape_is_injection_safe():
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert workflow["run-name"] == "ob-${{ inputs.action }}-${{ inputs.request_id }}"
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"action", "flow", "deployment_id", "params_json", "run_id", "request_id"}
    assert inputs["action"]["type"] == "choice" and set(inputs["action"]["options"]) == {"trigger", "status", "terminate", "list_runs"}
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    assert workflow["concurrency"]["group"] == "ob-dispatch-${{ inputs.request_id }}"
    steps = workflow["jobs"]["dispatch"]["steps"]
    for step in steps:
        script = step.get("run", "")
        # No expression at all inside a shell script: inputs reach the script only via env.
        assert "${{" not in script, step
        if "uses" in step:
            assert re.search(r"@(v\d+|[0-9a-f]{40})$", step["uses"]), step["uses"]
    run_step = next(step for step in steps if step.get("run") == "python scripts/ob_dispatch.py")
    assert {f"OB_{name.upper()}": f"${{{{ inputs.{name} }}}}" for name in inputs}.items() <= run_step["env"].items()
    login = next(step for step in steps if "service-principal-configure" in step.get("run", ""))
    for flag in ("--name mlops-dashboard-sp", "--deployment-domain augury.obp.outerbounds.com", "--perimeter default", "--github-actions"):
        assert flag in login["run"]
    install = next(step for step in steps if "pip install" in step.get("run", ""))
    assert "-r .github/ob-dispatch-requirements.txt" in install["run"]
    pins = (ROOT / ".github" / "ob-dispatch-requirements.txt").read_text(encoding="utf-8").split()
    for pin in ("outerbounds[azure]==0.12.39", "ob-metaflow==2.19.34.1", "ob-metaflow-extensions==1.6.24"):
        assert pin in pins
    assert not any(line.split("==")[0] == "metaflow" for line in pins)
    setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-python@"))
    assert setup["with"]["cache"] == "pip" and setup["with"]["cache-dependency-path"] == ".github/ob-dispatch-requirements.txt"
    names = [step.get("name", step.get("uses", "")) for step in steps]
    assert names.index("Mask parameter values in the log") < names.index("Run action")
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "ob-result-${{ inputs.request_id }}" and upload["if"] == "always()"
    assert int(upload["with"]["retention-days"]) <= 7
    ensure = next(step for step in steps if "--ensure-result" in step.get("run", ""))
    assert ensure["if"] == "always()"
    assert "github.event.inputs" not in text
    assert "@main" not in text and "pull_request" not in text
