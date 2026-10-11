"""Databricks workflow backend (Metaflow client) - every Metaflow call is mocked.

The end-to-end tests run the real child process (``python -m
backfill_dashboard.outerbounds_actions``) against a *fake* ``metaflow`` package
put first on PYTHONPATH, so nothing can reach Outerbounds or Argo.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from backfill_dashboard import outerbounds_actions as actions_module
from backfill_dashboard.admin import (
    AdminActionRepository,
    FullRlblTestParams,
    LogLookupParams,
    PROD_CONFIRMATION,
    RunningWorkflowProvider,
    STOP_PROD_CONFIRMATION,
    TerminateParams,
    TriggerParams,
    WorkflowCommandBuilder,
    WorkflowSourceRequest,
    ULRPM_ORCHESTRATOR_CONTRACT_VERSION,
)
from backfill_dashboard.control_store import JsonControlStore
from backfill_dashboard.manifests import OrchestratedManifestWriteResult
from backfill_dashboard.outerbounds_actions import (
    ACTION_MARKER,
    CampaignDispatchNotSupported,
    DeploymentNotConfigured,
    MetaflowCampaignAdapter,
    MetaflowCommandBuilder,
    MetaflowRunningWorkflowProvider,
    OperationNotSupported,
    ServicePrincipalNotConfigured,
    WorkflowBackendError,
    argo_submit_parameters,
    http_status_for,
    trigger_kwargs,
)
from backfill_dashboard.runtime_mode import SP_NOT_CONFIGURED, runtime_info
from backfill_dashboard.schemas import OrchestratedBackfillRequest

MACHINE = "683ec59079fecb5a5a240478"
OTHER_MACHINE = "6817571193e37ef05fffcd1c"
SP_ENV = {
    "MLOPS_DASHBOARD_RUNTIME": "databricks",
    "METAFLOW_SERVICE_AUTH_KEY": "sp-key-not-real-123456",
    "OBP_METAFLOW_CONFIG_URL": "https://api.example.invalid/config",
    "OUTERBOUNDS_FSTBACKFILL_DEPLOYMENT_ID": "fstbackfill.prod.fstbackfill",
    "OUTERBOUNDS_ULRPM_ORCHESTRATOR_DEPLOYMENT_ID": "fstbkfill.prod.ulrpmorchestrator",
    "OUTERBOUNDS_FULLRLBL_TEST_DEPLOYMENT_ID": "fullrsttest.test.fullrlbl",
}
# The Metaflow backend explicitly (without a key the Databricks default is now github_actions).
NO_SP_ENV = {"MLOPS_DASHBOARD_RUNTIME": "databricks", "DATABRICKS_APP_PORT": "8000", "OUTERBOUNDS_DISPATCH_BACKEND": "metaflow"}


# --- fake metaflow package (child-process tests) ---------------------------------------------

FAKE_METAFLOW = {
    "metaflow/__init__.py": '''
        import json, os

        def _log(entry):
            with open(os.environ["FAKE_MF_LOG"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\\n")

        def _mode():
            return os.environ.get("FAKE_MF_MODE", "")

        def namespace(value):
            _log({"call": "namespace", "value": value})

        class _Deployer:
            def __init__(self, identifier):
                self.name = identifier
                self.flow_name = "UlrpmDevBackfillOrchestratorFlow" if "ulrpm" in identifier else "FSTBackfill"

        class _Triggered:
            def __init__(self, pathspec):
                self.pathspec = pathspec

            def terminate(self, **kwargs):
                _log({"call": "terminate", "pathspec": self.pathspec, "authorize": bool(kwargs.get("authorize"))})
                return _mode() != "terminate_fail"

        class DeployedFlow:
            def __init__(self, identifier):
                self.deployer = _Deployer(identifier)

            @classmethod
            def from_deployment(cls, identifier, metadata=None, impl="argo_workflows"):
                _log({"call": "from_deployment", "identifier": identifier})
                if _mode() == "missing_deployment":
                    from metaflow.exception import MetaflowException
                    raise MetaflowException("No deployed flow found for: %s" % identifier)
                return cls(identifier)

            @classmethod
            def get_triggered_run(cls, identifier, run_id, metadata=None, impl="argo_workflows"):
                _log({"call": "get_triggered_run", "identifier": identifier, "run_id": run_id})
                return _Triggered(cls(identifier).deployer.flow_name + "/" + run_id)

            def trigger(self, **kwargs):
                _log({"call": "trigger", "identifier": self.deployer.name, "kwargs": kwargs})
                return _Triggered(self.deployer.flow_name + "/argo-" + self.deployer.name.split(".")[-1] + "-x7k2p")

        class _Run:
            def __init__(self, run_id, finished, successful):
                self.id, self.finished, self.successful = run_id, finished, successful
                self.created_at = "2026-10-11T06:00:00"
                self.finished_at = "2026-10-11T07:00:00" if finished else None

        RUNS = {
            "FSTBackfill": [_Run("argo-fstbackfill-done", True, True), _Run("argo-fstbackfill-bad", True, False)],
            "UlrpmDevBackfillOrchestratorFlow": [_Run("argo-ulrpm-live", False, False)],
        }

        class Flow:
            def __init__(self, name):
                from metaflow.exception import MetaflowNotFound
                if name not in RUNS:
                    raise MetaflowNotFound("Flow('%s') does not exist" % name)
                self.name = name

            def runs(self):
                _log({"call": "runs", "flow": self.name})
                return iter(RUNS[self.name])

        def Run(pathspec, _namespace_check=True):
            from metaflow.exception import MetaflowNotFound
            flow, run_id = pathspec.split("/")
            for run in RUNS.get(flow, []):
                if run.id == run_id:
                    return run
            raise MetaflowNotFound(pathspec)

        class Task:
            def __init__(self, pathspec, _namespace_check=True):
                from metaflow.exception import MetaflowNotFound
                if not pathspec.startswith("FSTBackfill/"):
                    raise MetaflowNotFound(pathspec)
                self.stdout = "hello from " + pathspec
                self.stderr = "warn"
    ''',
    "metaflow/exception.py": '''
        class MetaflowException(Exception):
            pass

        class MetaflowNotFound(MetaflowException):
            pass
    ''',
    "metaflow/metaflow_config.py": 'KUBERNETES_NAMESPACE = "jobs-default"\n',
    "metaflow/plugins/__init__.py": "",
    "metaflow/plugins/argo/__init__.py": "",
    "metaflow/plugins/argo/argo_client.py": '''
        import json, os
        from metaflow import _log, _mode

        PARAMS = {
            "name-space": {"name": "name-space", "type": "str", "description": "", "is_required": False},
            "storage_account_manifest_path": {"name": "storage_account_manifest_path", "type": "str", "description": "", "is_required": True},
            "manifest_bucket_count": {"name": "manifest_bucket_count", "type": "int", "description": "", "is_required": False},
            "force_sessions_from_bucket": {"name": "force_sessions_from_bucket", "type": "bool", "description": "", "is_required": False},
            "features_to_backfill": {"name": "features_to_backfill", "type": "str", "description": "", "is_required": False},
        }

        class ArgoClient:
            def __init__(self, namespace=None):
                if _mode() == "argo_forbidden":
                    raise Exception("403 Forbidden")

            def get_workflow_template(self, name):
                _log({"call": "get_workflow_template", "name": name})
                return {
                    "metadata": {"annotations": {
                        "metaflow/flow_name": "FSTBackfill",
                        "metaflow/production_token": "SECRET-PROD-TOKEN",
                        "metaflow/parameters": json.dumps({} if "ulrpm" in name else PARAMS),
                    }},
                    "spec": {"arguments": {"parameters": [
                        {"name": "orchestrator_contract_version", "value": json.dumps(os.environ.get("FAKE_MF_CONTRACT", "2026-10-06-v5"))},
                    ]}},
                }

            def get_workflow(self, name):
                _log({"call": "get_workflow", "name": name})
                if name == "gone":
                    return None
                return {
                    "metadata": {"name": name, "namespace": "jobs-default",
                                 "annotations": {"metaflow/flow_name": "UlrpmDevBackfillOrchestratorFlow",
                                                 "metaflow/run_id": "argo-" + name,
                                                 "metaflow/production_token": "SECRET-PROD-TOKEN"},
                                 "creationTimestamp": "2026-10-11T06:00:00Z"},
                    "status": {"phase": "Terminated" if name == "ulrpm-stopped" else "Running"},
                    "spec": {"arguments": {"parameters": [
                        {"name": "machine_ids", "value": json.dumps("683ec59079fecb5a5a240478")},
                        {"name": "machine_manifest_plan", "value": "x" * 9000},
                    ]}},
                }
    ''',
    "metaflow/plugins/argo/argo_workflows.py": '''
        from metaflow import _log

        class ArgoWorkflows:
            @classmethod
            def get_execution(cls, name):
                _log({"call": "get_execution", "name": name})
                if name == "gone":
                    return None
                return ("someone", "SECRET-PROD-TOKEN", "UlrpmDevBackfillOrchestratorFlow", None, None)
    ''',
}


@pytest.fixture()
def fake_metaflow(tmp_path, monkeypatch):
    root = tmp_path / "fake_site"
    for relative, body in FAKE_METAFLOW.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    log = tmp_path / "metaflow_calls.jsonl"
    backend = Path(actions_module.__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(root), str(backend)]))
    monkeypatch.setenv("FAKE_MF_LOG", str(log))
    monkeypatch.delenv("FAKE_MF_MODE", raising=False)
    # No remote-config bootstrap in the child (it would make an HTTP call).
    monkeypatch.delenv("OBP_METAFLOW_CONFIG_URL", raising=False)
    monkeypatch.setenv("METAFLOW_SERVICE_AUTH_KEY", "sp-key-not-real-123456")
    monkeypatch.setenv("MONGODB_URL", "mongodb+srv://user:pw@unrelated.invalid/db")
    monkeypatch.setenv("OUTERBOUNDS_PROBE_PYTHON", sys.executable)

    def calls():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return SimpleNamespace(root=root, calls=calls, log=log)


def _builder(env=SP_ENV, **kwargs):
    return MetaflowCommandBuilder(environ=dict(env), **kwargs)


def _run_action(tmp_path, cwd, command, action="trigger"):
    repository = AdminActionRepository(tmp_path / "actions.json")
    created = repository.create(action=action, command=command, cwd=cwd)
    repository.run(created.id)
    return repository.get(created.id)


# --- runtime gating ---------------------------------------------------------------------------

def test_databricks_without_sp_returns_clear_503_on_every_admin_mutation(monkeypatch):
    from test_runtime_mode import _request

    from backfill_dashboard import app as app_module

    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info(NO_SP_ENV))
    monkeypatch.setattr(app_module.admin_repository, "create", lambda *a, **k: pytest.fail("side effect before SP check"))
    for path in (
        "/api/admin/workflows/trigger",
        "/api/admin/backfills/orchestrated",
        "/api/admin/backfills/all",
        "/api/admin/fullrlbl/trigger",
        "/api/admin/workflows/terminate",
        "/api/admin/logs",
        "/api/admin/workflows/create",
        "/api/machines/machine-a/backfills/202608/cancel",
        "/api/v1/backfill-campaigns",
    ):
        response = _request(app_module.app, "POST", path, {})
        assert response.status_code == 503, (path, response.body)
        assert response.body["error_type"] == SP_NOT_CONFIGURED
        assert response.body["detail"].startswith("Outerbounds service principal not configured: set METAFLOW_SERVICE_AUTH_KEY and OBP_METAFLOW_CONFIG_URL")
        assert "monitor-only" not in response.body["detail"].lower()

    readiness = _request(app_module.app, "GET", "/api/admin/readiness").body
    assert readiness["argo"]["ready"] is False and readiness["argo"]["error_type"] == SP_NOT_CONFIGURED
    assert readiness["fullrlbl_test"]["error_type"] == SP_NOT_CONFIGURED
    running = _request(app_module.app, "GET", "/api/admin/workflows/running").body
    assert running["argo_available"] is False and "service principal" in running["hint"]


def test_databricks_with_sp_lets_admin_routes_through_to_validation(monkeypatch):
    from test_runtime_mode import _request

    from backfill_dashboard import app as app_module

    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info(SP_ENV))
    # An invalid body now reaches FastAPI validation (422) instead of the runtime gate.
    response = _request(app_module.app, "POST", "/api/admin/workflows/terminate", {"params": {"workflow_id": 5}})
    assert response.status_code in {400, 422}, response.body
    assert runtime_info(SP_ENV).as_dict()["capabilities"]["workflow_mutations"] is True


def test_factory_wires_metaflow_backend_only_on_databricks(monkeypatch, tmp_path):
    from dataclasses import replace

    from backfill_dashboard import factory
    from backfill_dashboard.config import get_settings

    # Settings defaults are read at import; never touch the developer's real /tmp state.
    settings = replace(get_settings(), control_plane_db_path=tmp_path / "cp.sqlite3",
                       state_path=tmp_path / "state.json", dispatch_enabled=True)
    monkeypatch.setattr(factory, "get_settings", lambda: settings)
    monkeypatch.setenv("BACKFILL_CONTROL_STORE", "local_json")
    monkeypatch.setattr(factory, "build_lifecycle_provider", lambda: (None, "off"))
    monkeypatch.setattr(factory, "log_startup_data_sources", lambda *a, **k: None)

    monkeypatch.setenv("MLOPS_DASHBOARD_RUNTIME", "local")
    local = factory.build_components()
    assert type(local[5]) is WorkflowCommandBuilder
    assert type(local[7]) is RunningWorkflowProvider

    for key, value in SP_ENV.items():
        monkeypatch.setenv(key, value)
    hosted = factory.build_components()
    assert isinstance(hosted[5], MetaflowCommandBuilder)
    assert isinstance(hosted[7], MetaflowRunningWorkflowProvider)
    assert isinstance(hosted[10].runtime.dispatcher.adapter, MetaflowCampaignAdapter)


# --- builder: same parameters as the argo path ---------------------------------------------

def test_trigger_parameters_are_identical_to_the_argo_submit_path():
    params = TriggerParams(namespace="ulrpm-fst-dev-x", storage_account_manifest_path="m/a.parquet",
                           include_features_to_backfill=True, features_to_backfill=["a", "b"], max_parallel_steps=3)
    _, argo = WorkflowCommandBuilder().build_trigger(WorkflowSourceRequest(), params)
    cwd, command = _builder().build_trigger(WorkflowSourceRequest(), params)
    request = json.loads(command[-1])
    assert command[1:3] == ["-m", "backfill_dashboard.outerbounds_actions"]
    assert request["op"] == "trigger" and request["deployment_id"] == SP_ENV["OUTERBOUNDS_FSTBACKFILL_DEPLOYMENT_ID"]
    assert cwd.is_dir()
    # What Metaflow's ArgoClient sends is json.dumps(value) per parameter: byte-identical to `argo submit -p`.
    argo_values = dict(item.split("=", 1) for item in argo[argo.index("-p"):] if item != "-p")
    assert {name: json.dumps(value) for name, value in request["parameters"].items()} == argo_values
    assert "sp-key" not in " ".join(command)


def test_orchestrator_trigger_keeps_sequential_machine_lanes_and_plan():
    plan = {MACHINE: [{"month_index": 17, "manifest": "p/a.parquet", "since": "2026/01/01/00", "until": "2026/01/08/00"},
                      {"month_index": 17, "manifest": "p/b.parquet", "since": "2026/01/08/00", "until": "2026/01/15/00"}]}
    kwargs = dict(machine_ids=[MACHINE, OTHER_MACHINE], manifest_template="p/m_{month_index}.parquet", start_index=17,
                  end_index=18, params=TriggerParams(max_parallel_steps=2),
                  month_indices_by_machine={MACHINE: [17], OTHER_MACHINE: [17]}, manifest_plan=plan)
    _, argo = WorkflowCommandBuilder().build_ulrpm_orchestrator_trigger(**kwargs)
    _, command = _builder().build_ulrpm_orchestrator_trigger(**kwargs)
    request = json.loads(command[-1])
    parameters = request["parameters"]
    assert request["deployment_id"] == SP_ENV["OUTERBOUNDS_ULRPM_ORCHESTRATOR_DEPLOYMENT_ID"]
    assert parameters["child_max_parallel_steps"] == 1  # same-machine chunks strictly sequential
    assert parameters["global_machine_concurrency"] == 2
    assert json.loads(parameters["machine_manifest_plan"]) == plan
    argo_values = dict(item.split("=", 1) for item in argo if "=" in item)
    assert all(json.dumps(parameters[name]) == argo_values[name] for name in parameters)
    with pytest.raises(ValueError, match="cannot exceed the selected machine count"):
        _builder().build_ulrpm_orchestrator_trigger(**{**kwargs, "params": TriggerParams(max_parallel_steps=3)})


def test_builder_requires_sp_and_deployment_ids_with_clear_errors():
    params = TriggerParams(storage_account_manifest_path="m/a.parquet")
    with pytest.raises(ServicePrincipalNotConfigured, match="Outerbounds service principal not configured") as sp_error:
        _builder(NO_SP_ENV).build_trigger(WorkflowSourceRequest(), params)
    assert sp_error.value.status_code == 503
    env = {key: value for key, value in SP_ENV.items() if key != "OUTERBOUNDS_FSTBACKFILL_DEPLOYMENT_ID"}
    with pytest.raises(DeploymentNotConfigured, match="OUTERBOUNDS_FSTBACKFILL_DEPLOYMENT_ID is not set") as dep_error:
        _builder(env).build_trigger(WorkflowSourceRequest(), params)
    assert dep_error.value.status_code == 503
    # Validation is shared with the argo path (prod confirmation still enforced).
    with pytest.raises(ValueError, match=PROD_CONFIRMATION):
        _builder().build_trigger(WorkflowSourceRequest(), TriggerParams(environment="prod", storage_account_manifest_path="m"))
    readiness = _builder(NO_SP_ENV).platform_readiness()
    assert readiness["ready"] is False and readiness["error_type"] == SP_NOT_CONFIGURED


def test_terminate_scope_create_and_logs_on_databricks():
    builder = _builder()
    _, command = builder.build_terminate(TerminateParams(workflow_id="argo-ulrpm-live"))
    request = json.loads(command[-1])
    assert request["op"] == "terminate" and request["workflow_id"] == "ulrpm-live"
    assert set(request["deployment_ids"]) == {SP_ENV[key] for key in actions_module.DEPLOYMENT_ENV.values()}
    with pytest.raises(OperationNotSupported, match="local-only") as unsupported:
        builder.build_terminate(TerminateParams(workflow_id=""))
    assert unsupported.value.status_code == 501
    with pytest.raises(ValueError, match=STOP_PROD_CONFIRMATION):
        builder.build_terminate(TerminateParams(environment="prod", workflow_id="argo-x-123"))
    with pytest.raises(OperationNotSupported, match="not available on Databricks"):
        builder.build_create(WorkflowSourceRequest())
    _, logs = builder.build_logs(WorkflowSourceRequest(), LogLookupParams(run_task_path="argo-a/start/1", stream="stderr"))
    assert json.loads(logs[-1])["stream"] == "stderr"
    _, fullrlbl = builder.build_fullrlbl_test_trigger(FullRlblTestParams(machine_ids=[MACHINE]))
    assert json.loads(fullrlbl[-1])["deployment_id"] == SP_ENV["OUTERBOUNDS_FULLRLBL_TEST_DEPLOYMENT_ID"]


def test_trigger_kwargs_coerce_to_declared_types_and_reject_unknown():
    info = {"ns": {"name": "name-space", "type": "str"}, "n": {"name": "count", "type": "int"},
            "j": {"name": "plan", "type": "JSON"}, "b": {"name": "flag", "type": "bool"}}
    assert trigger_kwargs({"name-space": "x", "count": "3", "plan": '{"a":1}', "flag": "false"}, info) == {
        "name_space": "x", "count": 3, "plan": {"a": 1}, "flag": False}
    with pytest.raises(ValueError, match="no parameter 'other'"):
        trigger_kwargs({"other": 1}, info)
    assert argo_submit_parameters(["argo", "submit", "-p", 'a="x"', "-p", "b=2", "-p", "c=false", "-n", "ns"]) == {"a": "x", "b": 2, "c": False}


@pytest.mark.parametrize("error_type,message,status", [
    ("metaflow.exception.MetaflowNotFound", "Flow('X') does not exist", 404),
    ("metaflow.exception.MetaflowException", "No deployed flow found for: abc", 404),
    ("metaflow.plugins.argo.argo_workflows_cli.RunIdMismatch", "belongs to another flow", 409),
    ("TimeoutError", "timed out after 60s", 504),
    ("ValueError", "deployment has no parameter 'x'", 422),
    ("metaflow.plugins.argo.argo_client.ArgoClientException", "403 Forbidden", 502),
    ("RuntimeError", "anything else", 502),
])
def test_metaflow_exceptions_map_to_http_statuses(error_type, message, status):
    assert http_status_for(error_type, message) == status


def test_backend_errors_keep_their_status_in_routes(monkeypatch):
    from fastapi import BackgroundTasks, HTTPException

    from backfill_dashboard import app as app_module
    from backfill_dashboard.schemas import TerminateWorkflowRequest

    monkeypatch.setattr(app_module, "command_builder", _builder())
    monkeypatch.setattr(app_module, "_require_argo_access", lambda: None)
    with pytest.raises(HTTPException) as error:
        app_module.terminate_workflow(TerminateWorkflowRequest(), BackgroundTasks())
    assert error.value.status_code == 501 and "local-only" in error.value.detail
    monkeypatch.setattr(app_module, "command_builder", _builder(NO_SP_ENV))
    with pytest.raises(HTTPException) as sp_error:
        app_module.terminate_workflow(TerminateWorkflowRequest(params={"workflow_id": "argo-abc-123"}), BackgroundTasks())
    assert sp_error.value.status_code == 503 and sp_error.value.detail.startswith("Outerbounds service principal not configured")


# --- child process with the fake metaflow -------------------------------------------------

def test_trigger_runs_in_child_and_records_workflow_id(fake_metaflow, tmp_path):
    params = TriggerParams(namespace="ulrpm-fst-dev-x", storage_account_manifest_path="m/a.parquet", max_parallel_steps=2)
    cwd, command = _builder().build_trigger(WorkflowSourceRequest(), params)
    action = _run_action(tmp_path, cwd, command)
    assert action["status"] == "running" and action["phase"] == "accepted", (action["stdout"], action["stderr"])
    assert action["workflow_id"] == "fstbackfill-x7k2p"
    trigger = next(call for call in fake_metaflow.calls() if call["call"] == "trigger")
    assert trigger["identifier"] == SP_ENV["OUTERBOUNDS_FSTBACKFILL_DEPLOYMENT_ID"]
    assert trigger["kwargs"] == {"name_space": "ulrpm-fst-dev-x", "storage_account_manifest_path": "m/a.parquet",
                                 "manifest_bucket_count": 2, "force_sessions_from_bucket": False, "features_to_backfill": ""}
    assert "sp-key-not-real" not in action["stdout"] + action["stderr"]


def test_trigger_failure_surfaces_metaflow_error_as_action_result(fake_metaflow, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MF_MODE", "missing_deployment")
    cwd, command = _builder().build_trigger(WorkflowSourceRequest(), TriggerParams(storage_account_manifest_path="m"))
    action = _run_action(tmp_path, cwd, command)
    assert action["status"] == "failed" and action["phase"] == "failed"
    assert "MetaflowException: No deployed flow found for: fstbackfill.prod.fstbackfill" in action["error"]


def test_child_environment_drops_unrelated_secrets(fake_metaflow, monkeypatch, tmp_path):
    # Work on a copy of the environment and restore the cwd: _prepare_child mutates both.
    environ = dict(os.environ, FST_PROD_ACCOUNT_KEY="unrelated-key", DATABRICKS_CLIENT_SECRET="unrelated")
    monkeypatch.setattr(os, "environ", environ)
    monkeypatch.chdir(tmp_path)
    actions_module._prepare_child({"op": "list"})
    assert not {"MONGODB_URL", "FST_PROD_ACCOUNT_KEY", "DATABRICKS_CLIENT_SECRET"} & set(environ)
    assert environ["METAFLOW_SERVICE_AUTH_KEY"]  # the SP credential itself is kept for Metaflow


def test_terminate_supported_via_triggered_run(fake_metaflow, tmp_path):
    cwd, command = _builder().build_terminate(TerminateParams(workflow_id="ulrpm-live"))
    action = _run_action(tmp_path, cwd, command, action="terminate")
    assert action["status"] == "succeeded", action["stdout"]
    terminate = next(call for call in fake_metaflow.calls() if call["call"] == "terminate")
    assert terminate == {"call": "terminate", "pathspec": "UlrpmDevBackfillOrchestratorFlow/argo-ulrpm-live", "authorize": False}
    assert "SECRET-PROD-TOKEN" not in action["stdout"]


def test_terminate_rejected_by_production_token_is_explained(fake_metaflow, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MF_MODE", "terminate_fail")
    cwd, command = _builder().build_terminate(TerminateParams(workflow_id="ulrpm-live"))
    action = _run_action(tmp_path, cwd, command, action="terminate")
    assert action["status"] == "failed"
    assert "production token" in action["error"] and "OUTERBOUNDS_TERMINATE_AUTHORIZE_TOKEN" in action["error"]


def test_status_list_and_lookup_through_child(fake_metaflow):
    provider = MetaflowRunningWorkflowProvider(environ=dict(SP_ENV))
    recent = provider.list_recent()
    by_id = {item["workflow_id"]: item for item in recent["workflows"]}
    assert recent["argo_available"] is True and recent["workflow_backend"] == "metaflow"
    assert by_id["fstbackfill-done"]["status"] == "Succeeded"
    assert by_id["fstbackfill-bad"]["status"] == "Failed"
    assert by_id["ulrpm-live"]["status"] == "Running"
    assert by_id["ulrpm-live"]["machine_ids"] == [MACHINE]  # from the submitted Argo parameters
    assert recent["counts"] == {"active": 1, "completed": 1, "failed": 1}
    running = provider.list_running()["workflows"]
    assert [item["workflow_id"] for item in running] == ["ulrpm-live"]
    looked_up = provider.lookup_workflows(["argo-ulrpm-stopped", "gone"])
    assert [(item["workflow_id"], item["status"]) for item in looked_up] == [("ulrpm-stopped", "Terminated")]
    calls = fake_metaflow.calls()
    assert {"call": "namespace", "value": None} in calls
    assert not any(call["call"] in {"trigger", "terminate"} for call in calls)


def test_status_falls_back_to_metaflow_run_state_when_argo_is_forbidden(fake_metaflow, monkeypatch):
    monkeypatch.setenv("FAKE_MF_MODE", "argo_forbidden")
    provider = MetaflowRunningWorkflowProvider(environ=dict(SP_ENV))
    recent = provider.list_recent()
    by_id = {item["workflow_id"]: item for item in recent["workflows"]}
    assert by_id["ulrpm-live"]["status"] == "Running"
    assert recent["warnings"]
    assert [item["workflow_id"] for item in provider.lookup_workflows(["fstbackfill-done"])] == ["fstbackfill-done"]


def test_orchestrator_readiness_reads_contract_through_child(fake_metaflow, monkeypatch):
    readiness = _builder().ulrpm_orchestrator_readiness()
    assert readiness["ready"] is True and readiness["supports_submonth_split"] is True
    assert readiness["contract_version"] == ULRPM_ORCHESTRATOR_CONTRACT_VERSION and readiness["backend"] == "metaflow"
    monkeypatch.setenv("FAKE_MF_CONTRACT", "2020-01-01-v1")
    stale = _builder().ulrpm_orchestrator_readiness()
    assert stale["ready"] is False and "stale" in stale["detail"]


def test_logs_through_child(fake_metaflow, tmp_path):
    cwd, command = _builder().build_logs(WorkflowSourceRequest(), LogLookupParams(run_task_path="argo-fstbackfill-done/start/1"))
    action = _run_action(tmp_path, cwd, command, action="logs")
    assert action["status"] == "succeeded"
    assert "hello from FSTBackfill/argo-fstbackfill-done/start/1" in action["stdout"]


def test_provider_reports_sp_missing_and_backend_errors_without_raising():
    missing = MetaflowRunningWorkflowProvider(environ=dict(NO_SP_ENV))
    assert missing.list_recent()["argo_available"] is False
    assert missing.lookup_workflows(["abc-123"]) == []

    def failing(*_args, **_kwargs):
        raise WorkflowBackendError("upstream down", status_code=502)

    broken = MetaflowRunningWorkflowProvider(environ=dict(SP_ENV), runner=failing)
    assert broken.list_running()["error"] == "upstream down"
    assert broken.lookup_workflows(["abc-123"]) == []


# --- duplicate submit / sequential guards on the Databricks path ---------------------------

def _queued_orchestrated(monkeypatch, tmp_path, builder):
    import backfill_dashboard.app as app_module
    from fastapi import BackgroundTasks

    monkeypatch.setattr(app_module, "BACKFILL_CANCEL_WINDOW_SECONDS", 0)
    month_index = 17

    class SnapshotRepository:
        def latest(self):
            return {"snapshot": {"machines": [{"machine_id": machine, "months": [{
                "status": "needs_backfill", "activity_status": "online", "row_count": 12,
                "partition": {"year": 2026, "month": 1}}]} for machine in (MACHINE, OTHER_MACHINE)]}}

    class Writer:
        windows = {}

        def write_orchestrated_monthly_manifests(self, **kwargs):
            return OrchestratedManifestWriteResult(
                account_name="test", container_name="test", manifest_prefix="test/run",
                manifest_template="test/run/month_{month_index}.parquet",
                machine_ids=kwargs["machine_ids"], start_index=month_index, end_index=month_index + 1,
                month_count=1, manifest_count=1, blob_url="https://example.invalid/test/run",
                month_indices_by_machine=kwargs["month_indices_by_machine"],
                windows_by_machine=self.windows, split={"mode": "month", "days": None},
            )

    repository = AdminActionRepository(tmp_path / "actions.json")
    control = JsonControlStore(tmp_path / "control.json")
    writer = Writer()
    monkeypatch.setattr(app_module, "admin_repository", repository)
    monkeypatch.setattr(app_module, "repository", SnapshotRepository())
    monkeypatch.setattr(app_module, "manifest_writer", writer)
    monkeypatch.setattr(app_module, "command_builder", builder)
    monkeypatch.setattr(app_module, "control_store", control)
    monkeypatch.setattr(app_module, "_require_ulrpm_orchestrator_access", lambda: None)

    def submit(machine):
        background = BackgroundTasks()
        response = app_module.trigger_orchestrated_machine_backfill(
            OrchestratedBackfillRequest(machine_ids=[machine], month_indices_by_machine={machine: [month_index]}), background)
        return response, background

    return SimpleNamespace(app=app_module, repository=repository, control=control, writer=writer, submit=submit, month=month_index)


def _run_background(background):
    for task in background.tasks:
        task.func(*task.args, **task.kwargs)


def test_databricks_orchestrated_submit_links_reservation_and_blocks_duplicates(fake_metaflow, monkeypatch, tmp_path):
    from fastapi import HTTPException

    ctx = _queued_orchestrated(monkeypatch, tmp_path, _builder())
    first, background = ctx.submit(MACHINE)
    # Duplicate submit while the first is still queued: the exclusive trigger guard answers 409.
    with pytest.raises(HTTPException) as duplicate:
        ctx.submit(OTHER_MACHINE)
    assert duplicate.value.status_code == 409 and "already" in duplicate.value.detail
    _run_background(background)
    action = ctx.repository.get(first["action"]["id"])
    assert action["phase"] == "accepted", action.get("error")
    assert action["workflow_id"] == "ulrpmorchestrator-x7k2p"
    assert ctx.control.get_month_state(MACHINE, ctx.month)["parent_workflow_id"] == "ulrpmorchestrator-x7k2p"
    triggers = [call for call in fake_metaflow.calls() if call["call"] == "trigger"]
    assert len(triggers) == 1 and triggers[0]["kwargs"]["child_max_parallel_steps"] == 1

    # Same machine again while its parent is active: rejected before any Metaflow call.
    second, background = ctx.submit(MACHINE)
    _run_background(background)
    rejected = ctx.repository.get(second["action"]["id"])
    assert rejected["status"] == "failed" and "already has an active backfill" in rejected["error"]
    assert len([call for call in fake_metaflow.calls() if call["call"] == "trigger"]) == 1


def test_databricks_path_rejects_overlapping_same_machine_chunks_before_trigger(monkeypatch, tmp_path):
    def no_trigger(*_args, **_kwargs):
        pytest.fail("overlapping windows reached the Metaflow trigger")

    builder = _builder()
    monkeypatch.setattr(builder, "_trigger_command", no_trigger)
    ctx = _queued_orchestrated(monkeypatch, tmp_path, builder)
    ctx.writer.windows = {MACHINE: [
        {"month_index": 17, "since": "2026/01/01/00", "until": "2026/01/10/00", "manifest_path": "a"},
        {"month_index": 17, "since": "2026/01/05/00", "until": "2026/01/15/00", "manifest_path": "b"},
    ]}
    response, background = ctx.submit(MACHINE)
    _run_background(background)
    action = ctx.repository.get(response["action"]["id"])
    assert action["status"] == "failed" and "chronological and non-overlapping" in action["error"]
    assert ctx.control.get_month_state(MACHINE, ctx.month) is None


def test_campaign_dispatch_on_databricks_is_refused_permanently(tmp_path):
    from backfill_dashboard.control_plane import ControlPlane

    settings = SimpleNamespace(control_plane_db_path=tmp_path / "cp.sqlite3", estimate_ttl_seconds=900,
                               dispatch_enabled=True, production_mode=False)
    plane = ControlPlane(settings, object(), "healthy", manifest_writer=None, workflow_mutations_enabled=True,
                         workflow_adapter=MetaflowCampaignAdapter())
    adapter = plane.runtime.dispatcher.adapter
    with pytest.raises(CampaignDispatchNotSupported, match="not supported on Databricks"):
        adapter.submit({"machine_id": MACHINE})
    assert isinstance(CampaignDispatchNotSupported("x"), ValueError)  # Dispatcher: permanent failure, no retry/find
    assert adapter.find({}) is None and adapter.statuses() == {}


def test_child_error_marker_is_parsed_and_cli_output_is_not():
    from backfill_dashboard.admin import _child_error

    line = ACTION_MARKER + json.dumps({"ok": False, "error_type": "metaflow.exception.MetaflowNotFound", "error": "nope"})
    assert _child_error(f"noise\n{line}\n") == "metaflow.exception.MetaflowNotFound: nope"
    assert _child_error("Name: fstbackfill-abc\nNamespace: jobs-default\n") is None
