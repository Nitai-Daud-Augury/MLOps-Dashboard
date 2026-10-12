from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from backfill_dashboard.runtime_mode import (
    RuntimeConfigurationError,
    resolve_runtime,
    runtime_info,
)
from backfill_dashboard.silver import _create_config
from backfill_dashboard.storage import AzureBlobDiscovery


def _request(app, method: str, path: str, payload: dict | None = None):
    async def run():
        sent = []
        body = json.dumps(payload or {}).encode()
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        await app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": method, "scheme": "http",
            "path": unquote(path), "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "client": ("testclient", 50000), "server": ("testserver", 80),
        }, receive, send)
        start = next(item for item in sent if item["type"] == "http.response.start")
        content = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        return SimpleNamespace(status_code=start["status"], body=json.loads(content))

    return asyncio.run(run())


def test_runtime_mode_explicit_and_inferred_selection():
    assert resolve_runtime({}) == "local"
    assert resolve_runtime({"DATABRICKS_APP_PORT": "8000"}) == "databricks"
    assert resolve_runtime({"DATABRICKS_APP_PORT": "8000", "MLOPS_DASHBOARD_RUNTIME": "docker"}) == "docker"
    assert resolve_runtime({"MLOPS_DASHBOARD_RUNTIME": " LOCAL "}) == "local"


def test_invalid_runtime_mode_fails_clearly():
    with pytest.raises(RuntimeConfigurationError, match="Invalid MLOPS_DASHBOARD_RUNTIME.*expected one of"):
        resolve_runtime({"MLOPS_DASHBOARD_RUNTIME": "cloud"})
    with pytest.raises(RuntimeConfigurationError, match="Invalid MLOPS_DASHBOARD_RUNTIME"):
        resolve_runtime({"MLOPS_DASHBOARD_RUNTIME": " "})


SP_CREDS = {"METAFLOW_SERVICE_AUTH_KEY": "sp-key-not-real-123456", "OBP_METAFLOW_CONFIG_URL": "https://api.example.invalid/c"}
GHA_TOKEN = {"GITHUB_DISPATCH_TOKEN": "github_pat_not_real_0123456789abcdef"}


@pytest.mark.parametrize("environ,enabled,backend,error_type", [
    ({"MLOPS_DASHBOARD_RUNTIME": "local"}, True, "argo_cli", None),
    ({"MLOPS_DASHBOARD_RUNTIME": "docker"}, False, None, "runtime_without_workflow_backend"),
    # Databricks without a Metaflow key defaults to the GitHub Actions dispatch backend.
    ({"MLOPS_DASHBOARD_RUNTIME": "databricks"}, False, "github_actions", "github_dispatch_not_configured"),
    ({"MLOPS_DASHBOARD_RUNTIME": "databricks", **GHA_TOKEN}, True, "github_actions", None),
    ({"MLOPS_DASHBOARD_RUNTIME": "databricks", "OUTERBOUNDS_DISPATCH_BACKEND": "metaflow"}, False, "metaflow",
     "outerbounds_sp_not_configured"),
    ({"MLOPS_DASHBOARD_RUNTIME": "databricks", **SP_CREDS}, True, "metaflow", None),
    # Explicit selection wins over the key-based default.
    ({"MLOPS_DASHBOARD_RUNTIME": "databricks", **SP_CREDS, "OUTERBOUNDS_DISPATCH_BACKEND": "github_actions"}, False,
     "github_actions", "github_dispatch_not_configured"),
    ({"MLOPS_DASHBOARD_RUNTIME": "databricks", **GHA_TOKEN, "OUTERBOUNDS_DISPATCH_BACKEND": " GitHub_Actions "}, True,
     "github_actions", None),
    ({"MLOPS_DASHBOARD_RUNTIME": "databricks", "OUTERBOUNDS_DISPATCH_BACKEND": "argo"}, False, None, "invalid_dispatch_backend"),
    # Local stays argo unless a backend is set explicitly.
    ({"MLOPS_DASHBOARD_RUNTIME": "local", **GHA_TOKEN}, True, "argo_cli", None),
    ({"MLOPS_DASHBOARD_RUNTIME": "local", **GHA_TOKEN, "OUTERBOUNDS_DISPATCH_BACKEND": "github_actions"}, True,
     "github_actions", None),
    ({"MLOPS_DASHBOARD_RUNTIME": "docker", **GHA_TOKEN}, False, None, "runtime_without_workflow_backend"),
    # Inferred Databricks Apps runtime behaves the same.
    ({"DATABRICKS_APP_PORT": "8000", **SP_CREDS}, True, "metaflow", None),
])
def test_runtime_capabilities_are_safe_and_workflow_gated(environ, enabled, backend, error_type):
    info = runtime_info(environ).as_dict()
    assert info["capabilities"] == {"monitor": True, "workflow_mutations": enabled, "manifest_creation": True}
    assert info["workflow_backend"] == backend
    assert info["workflow_mutations_error_type"] == error_type
    assert (info["workflow_mutations_error"] is None) == enabled
    assert "sp-key-not-real" not in repr(info) and "example.invalid" not in repr(info)
    assert "github_pat_not_real" not in repr(info)
    if error_type in {"github_dispatch_not_configured", "outerbounds_sp_not_configured"}:
        # Both options are named whichever backend is selected.
        assert "GITHUB_DISPATCH_TOKEN" in info["workflow_mutations_error"]
        assert "METAFLOW_SERVICE_AUTH_KEY" in info["workflow_mutations_error"]


@pytest.mark.parametrize("missing", ["METAFLOW_SERVICE_AUTH_KEY", "OBP_METAFLOW_CONFIG_URL"])
def test_databricks_needs_both_sp_keys_and_names_the_missing_one(missing):
    environ = {"MLOPS_DASHBOARD_RUNTIME": "databricks", "OUTERBOUNDS_DISPATCH_BACKEND": "metaflow", **SP_CREDS, missing: "  "}
    info = runtime_info(environ)
    assert info.workflow_mutations is False and info.workflow_mutations_status_code == 503
    assert info.workflow_mutations_error.startswith(
        "Outerbounds service principal not configured: set METAFLOW_SERVICE_AUTH_KEY and OBP_METAFLOW_CONFIG_URL"
    )
    assert f"missing: {missing}" in info.workflow_mutations_error


_MUTATION_ROUTES = [
    "/api/admin/workflows/preflight",
    "/api/admin/workflows/create",
    "/api/admin/workflows/trigger",
    "/api/admin/backfills/all",
    "/api/admin/backfills/orchestrated",
    "/api/admin/actions/action-a/cancel-before-submit",
    "/api/admin/workflows/terminate",
    "/api/admin/logs",
    "/api/machines/machine-a/backfills/202608/cancel",
    "/api/v1/backfill-campaigns",
    "/api/v1/backfill-campaigns/campaign-a/actions",
    "/api/v1/backfill-campaigns/campaign-a/items/item-a/actions",
    "/api/v1/backfill-campaigns/campaign-a/machines/machine-a/actions",
]


@pytest.mark.parametrize("environ,status,error_type", [
    ({"MLOPS_DASHBOARD_RUNTIME": "databricks"}, 503, "github_dispatch_not_configured"),
    ({"MLOPS_DASHBOARD_RUNTIME": "databricks", "OUTERBOUNDS_DISPATCH_BACKEND": "metaflow"}, 503, "outerbounds_sp_not_configured"),
    ({"MLOPS_DASHBOARD_RUNTIME": "docker"}, 403, "runtime_without_workflow_backend"),
])
def test_unavailable_workflow_routes_reject_before_side_effects(monkeypatch, environ, status, error_type):
    from backfill_dashboard import app as app_module

    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info(environ))
    def unexpected(*_args, **_kwargs):
        raise AssertionError("blocked route reached a side-effecting handler")

    monkeypatch.setattr(app_module.admin_repository, "create", unexpected)
    monkeypatch.setattr(app_module.command_builder, "build_trigger", unexpected)
    health = _request(app_module.app, "GET", "/api/health")
    assert health.status_code == 200 and health.body["runtime"]["mode"] == environ["MLOPS_DASHBOARD_RUNTIME"]
    public_spec = _request(app_module.app, "GET", "/api/admin/spec").body
    assert public_spec["runtime"]["capabilities"]["workflow_mutations"] is False
    assert public_spec["runtime"]["workflow_mutations_error_type"] == error_type
    assert "profile" not in public_spec["runtime"]
    assert not app_module._workflow_mutation_route("POST", "/api/admin/manifests/orchestrated")

    # Invalid/empty payloads prove the runtime middleware runs before request
    # validation and before action creation, reservations, or subprocess calls.
    for path in _MUTATION_ROUTES:
        response = _request(app_module.app, "POST", path, {})
        assert response.status_code == status, (path, response.body)
        assert response.body["error_type"] == error_type
        assert "monitor-only" not in response.body["detail"].lower()


def test_local_runtime_never_gates_workflow_routes(monkeypatch):
    from backfill_dashboard import app as app_module

    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info({"MLOPS_DASHBOARD_RUNTIME": "local"}))
    response = _request(app_module.app, "POST", "/api/admin/workflows/terminate", {"params": {"workflow_id": 5}})
    assert response.status_code in {400, 422}, response.body  # reached validation, not a runtime gate


def test_ui_has_no_monitor_only_banners_or_pre_disabled_admin_buttons():
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    text = "\n".join(path.read_text(encoding="utf-8") for path in src.rglob("*.tsx"))
    for phrase in ("Monitor-only mode", "monitor-only runtime", "disabled in monitor-only", "runtime-mode-notice"):
        assert phrase not in text, phrase
    assert "disabled={!workflowMutationsEnabled" not in text
    assert "disabled={!mutationsEnabled" not in text


def test_nonlocal_readiness_and_workflow_status_never_use_local_cli(monkeypatch):
    from backfill_dashboard import app as app_module

    monkeypatch.setattr(app_module, "RUNTIME_INFO", runtime_info({"MLOPS_DASHBOARD_RUNTIME": "docker"}))

    def unexpected(*_args, **_kwargs):
        raise AssertionError("hosted read endpoint attempted to invoke a local workflow CLI")

    monkeypatch.setattr(app_module.running_workflows, "list_recent", unexpected)
    monkeypatch.setattr(app_module.running_workflows, "list_running", unexpected)
    monkeypatch.setattr(app_module.running_workflows, "lookup_workflows", unexpected)
    monkeypatch.setattr(app_module.command_builder, "ulrpm_orchestrator_readiness", unexpected)
    monkeypatch.setattr(app_module.manifest_writer, "readiness", lambda: {"ready": True})

    for path in (
        "/api/admin/actions",
        "/api/admin/workflows/running",
        "/api/admin/workflows/summary",
        "/api/admin/readiness",
        "/api/backfill/runner-lock-status",
    ):
        response = _request(app_module.app, "GET", path)
        assert response.status_code == 200, (path, response.body)

    assert _request(app_module.app, "GET", "/api/admin/workflows/running").body["argo_available"] is False


def test_databricks_sql_config_skips_local_profile():
    constructed = []

    class ConfigStub:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    _create_config(ConfigStub, "developer-profile", mode="databricks")
    _create_config(ConfigStub, "developer-profile", mode="local")
    assert constructed == [{}, {"profile": "developer-profile"}]


def test_nonlocal_storage_discovery_uses_only_explicit_values(monkeypatch):
    monkeypatch.setenv("MLOPS_DASHBOARD_RUNTIME", "docker")
    monkeypatch.setenv("MLOPS_DASHBOARD_STORAGE_ACCOUNTS", "acctone, accttwo")
    monkeypatch.setenv("MLOPS_DASHBOARD_STORAGE_CONTAINERS", "fst-data, fst-test")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("nonlocal discovery attempted external/interactive discovery")

    import subprocess
    monkeypatch.setattr(subprocess, "run", unexpected)
    discovery = AzureBlobDiscovery("defaultacct", "defaultcontainer")
    assert discovery.list_accounts() == ["acctone", "accttwo"]
    assert discovery.list_containers("acctone") == ["fst-data", "fst-test"]
    assert discovery.list_containers("otheracct") == []


def test_deployed_control_plane_disables_campaign_dispatch(tmp_path, monkeypatch):
    from backfill_dashboard.control_plane import ControlPlane

    settings = SimpleNamespace(
        control_plane_db_path=tmp_path / "control.sqlite3",
        estimate_ttl_seconds=900,
        dispatch_enabled=True,
        production_mode=False,
    )
    plane = ControlPlane(settings, object(), "healthy", manifest_writer=None, workflow_mutations_enabled=False)
    assert plane.runtime.dispatcher is None


def test_outerbounds_probe_routes_are_not_workflow_mutations():
    from backfill_dashboard import app as app_module

    assert not app_module._workflow_mutation_route("POST", "/api/diagnostics/outerbounds")
    assert not app_module._workflow_mutation_route("GET", "/api/diagnostics/outerbounds/status")
