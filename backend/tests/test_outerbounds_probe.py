"""Outerbounds connectivity probe: gating, staged results, redaction.

No test reaches the network or runs Metaflow: sockets, HTTP and the Metaflow
child process are mocked.
"""
from __future__ import annotations

import json
import logging
import socket
import sys
from types import SimpleNamespace

import pytest

from backfill_dashboard import outerbounds_probe as probe
from test_runtime_mode import _request

TOKEN = "obp-test-token-0123456789abcdef"
CONFIG_URL = "https://api.augury.obp.outerbounds.com/v1/perimeters/default/abcdefghijklmnop0123/default"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path, caplog):
    for key in list(probe.CONFIG_ENV_KEYS) + [
        "OUTERBOUNDS_PROBE_ENABLED", "OUTERBOUNDS_PROBE_TRIGGER_ENABLED", "OUTERBOUNDS_PROBE_STAGE_TIMEOUT_SECONDS",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("METAFLOW_HOME", str(tmp_path / "mfhome"))  # empty: no credentials
    monkeypatch.setattr(probe, "_LAST_RESULT", None)
    caplog.set_level(logging.INFO, logger=probe.LOGGER_NAME)


def _ok_network(monkeypatch):
    monkeypatch.setattr(probe, "_check_host", lambda host, timeout: f"{host}: dns 1 addr(s), tcp ok, tls TLSv1.3")


class Child:
    """Fake child process: returns canned per-stage results and records calls."""

    def __init__(self, **by_stage):
        self.by_stage = by_stage
        self.calls: list[str] = []

    def __call__(self, stage, payload, cfg, secrets):
        self.calls.append(stage)
        return self.by_stage.get(stage, {"ok": True, "metadata": "local@/tmp", "metaflow_version": "2.19.34.1", "versions": {}})


def _http(code, text="", json_body=None):
    def get(url, headers, timeout):
        get.calls.append((url, dict(headers)))
        return SimpleNamespace(status_code=code, text=text, json=lambda: json_body if json_body is not None else {})

    get.calls = []
    return get


def _by_id(result):
    return {stage["id"]: stage for stage in result["stages"]}


def _with_token(monkeypatch, tmp_path):
    home = tmp_path / "mfhome"
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps({"METAFLOW_SERVICE_AUTH_KEY": TOKEN, "OBP_METAFLOW_CONFIG_URL": CONFIG_URL}))


# --- API gating ------------------------------------------------------------------

def test_flag_off_denies_both_routes(monkeypatch):
    from backfill_dashboard import app as app_module

    monkeypatch.setattr(probe, "run_probe", lambda *a, **k: pytest.fail("probe must not run when disabled"))
    assert _request(app_module.app, "GET", "/api/diagnostics/outerbounds/status").status_code == 404
    response = _request(app_module.app, "POST", "/api/diagnostics/outerbounds", {"flow": "NotAllowed"})
    assert response.status_code == 404
    assert "OUTERBOUNDS_PROBE_ENABLED" in response.body["detail"]


def test_non_allowlisted_flow_gets_422(monkeypatch):
    from backfill_dashboard import app as app_module

    monkeypatch.setenv("OUTERBOUNDS_PROBE_ENABLED", "1")
    monkeypatch.setattr(probe, "_run_child", lambda *a, **k: pytest.fail("no stage may run for a rejected flow"))
    response = _request(app_module.app, "POST", "/api/diagnostics/outerbounds", {"flow": "BX_Full_Flow"})
    assert response.status_code == 422
    assert "BX_Full_Flow" in response.body["detail"]
    bad_deployment = _request(
        app_module.app, "POST", "/api/diagnostics/outerbounds", {"flow": "FSTBackfill", "deployment_id": "fstbackfill.prod"}
    )
    assert bad_deployment.status_code == 422


def test_status_endpoint_exposes_flags_without_secrets(monkeypatch, tmp_path):
    from backfill_dashboard import app as app_module

    _with_token(monkeypatch, tmp_path)
    monkeypatch.setenv("OUTERBOUNDS_PROBE_ENABLED", "1")
    response = _request(app_module.app, "GET", "/api/diagnostics/outerbounds/status")
    assert response.status_code == 200
    body = response.body
    assert body["enabled"] is True and body["trigger_enabled"] is False
    assert body["flow_allowlist"] == ["FSTBackfill"]
    assert body["hosts"] == ["api.augury.obp.outerbounds.com", "metadata.augury.obp.outerbounds.com"]
    assert body["credentials_source"] == "config_file"
    assert TOKEN not in json.dumps(body) and "abcdefghijklmnop0123" not in json.dumps(body)


def test_api_run_returns_stage_table(monkeypatch):
    from backfill_dashboard import app as app_module

    monkeypatch.setenv("OUTERBOUNDS_PROBE_ENABLED", "1")
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_run_child", Child(list_runs={"ok": False, "metadata": "local@/x", "error_type": "metaflow.exception.MetaflowNotFound", "error": "Flow('FSTBackfill') does not exist"}))
    monkeypatch.setattr(probe, "_http_get", _http(403, "<html>403 Forbidden</html>"))
    response = _request(app_module.app, "POST", "/api/diagnostics/outerbounds", {})
    assert response.status_code == 200
    assert [stage["id"] for stage in response.body["stages"]] == ["network", "packages", "auth", "list_runs", "trigger"]
    assert set(response.body["stages"][0]) == {"id", "name", "status", "detail", "error_type", "ms"}
    assert response.body["overall"] == "expected_fail"
    status = _request(app_module.app, "GET", "/api/diagnostics/outerbounds/status").body
    assert status["last_result"]["probe_id"] == response.body["probe_id"]


# --- stages ----------------------------------------------------------------------

def test_dns_failure_is_reported_and_probe_continues(monkeypatch):
    def no_dns(host, port, proto=0):
        raise socket.gaierror(8, "nodename nor servname provided, or not known")

    monkeypatch.setattr(probe.socket, "getaddrinfo", no_dns)
    child = Child()
    monkeypatch.setattr(probe, "_run_child", child)
    monkeypatch.setattr(probe, "_http_get", _http(403))
    result = probe.run_probe()
    network = _by_id(result)["network"]
    assert network["status"] == "fail"
    assert network["error_type"] == "socket.gaierror"
    assert "api.augury.obp.outerbounds.com" in network["detail"] and "nodename" in network["detail"]
    # Later stages still ran (list_runs is always attempted).
    assert child.calls == ["packages", "list_runs"]
    assert result["overall"] == "fail"


def test_import_failure_marks_packages_failed(monkeypatch):
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_http_get", _http(403))
    monkeypatch.setattr(probe, "_run_child", Child(
        packages={"ok": False, "error_type": "ModuleNotFoundError", "error": "No module named 'metaflow'"},
        list_runs={"ok": False, "error_type": "ModuleNotFoundError", "error": "No module named 'metaflow'"},
    ))
    stages = _by_id(probe.run_probe())
    assert stages["packages"]["status"] == "fail"
    assert stages["packages"]["error_type"] == "ModuleNotFoundError"
    assert "No module named 'metaflow'" in stages["packages"]["detail"]
    assert "credentials: none" in stages["packages"]["detail"]


def test_child_main_reports_real_import_failure(monkeypatch):
    """The child-side code turns a missing metaflow into ok=False/ModuleNotFoundError."""
    monkeypatch.setitem(sys.modules, "metaflow", None)  # makes `import metaflow` raise
    out = probe.child_main({"stage": "packages", "timeout": 5})
    assert out["ok"] is False
    assert out["error_type"] in {"ModuleNotFoundError", "ImportError"}


def test_unauthenticated_403_is_expected_fail(monkeypatch):
    _ok_network(monkeypatch)
    http = _http(403, "<html>403 Forbidden</html>")
    monkeypatch.setattr(probe, "_http_get", http)
    monkeypatch.setattr(probe, "_run_child", Child(list_runs={"ok": False, "metadata": "local@/x", "error_type": "metaflow.exception.MetaflowNotFound", "error": "Flow('FSTBackfill') does not exist"}))
    stages = _by_id(probe.run_probe())
    assert stages["auth"]["status"] == "expected_fail"
    assert stages["auth"]["error_type"] == "HTTP403"
    assert http.calls == [("https://metadata.augury.obp.outerbounds.com/p/default/ping", {})]
    assert stages["list_runs"]["status"] == "expected_fail"
    assert stages["list_runs"]["error_type"] == "metaflow.exception.MetaflowNotFound"


def test_unauthenticated_401_is_expected_fail(monkeypatch):
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_http_get", _http(401))
    monkeypatch.setattr(probe, "_run_child", Child())
    assert _by_id(probe.run_probe())["auth"]["status"] == "expected_fail"


def test_rejected_token_is_fail_and_redacted(monkeypatch, tmp_path, caplog):
    _with_token(monkeypatch, tmp_path)
    _ok_network(monkeypatch)
    http = _http(403, f'{{"message": "bad key {TOKEN}", "x-api-key": "{TOKEN}"}}')
    monkeypatch.setattr(probe, "_http_get", http)
    monkeypatch.setattr(probe, "_run_child", Child(list_runs={"ok": False, "metadata": "service@x", "error_type": "metaflow.exception.MetaflowException", "error": f"403 for url {CONFIG_URL} with Authorization: Bearer {TOKEN}"}))
    result = probe.run_probe()
    stages = _by_id(result)
    assert stages["auth"]["status"] == "fail" and stages["auth"]["error_type"] == "HTTP403"
    assert stages["list_runs"]["status"] == "fail"
    # The token is sent only as the x-api-key header of the config call...
    assert http.calls[0][0] == CONFIG_URL and http.calls[0][1] == {"x-api-key": TOKEN}
    # ...and never appears in the response or the logs.
    dumped = json.dumps(result)
    logs = "\n".join(record.getMessage() for record in caplog.records)
    for text in (dumped, logs):
        assert TOKEN not in text
        assert "abcdefghijklmnop0123" not in text
        assert "Bearer obp" not in text


def test_accepted_token_passes_without_exposing_config(monkeypatch, tmp_path, caplog):
    _with_token(monkeypatch, tmp_path)
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_http_get", _http(200, json.dumps({"config": {"METAFLOW_SERVICE_AUTH_KEY": TOKEN}}),
                                                  {"config": {"METAFLOW_SERVICE_AUTH_KEY": TOKEN, "A": "b"}}))
    monkeypatch.setattr(probe, "_run_child", Child(list_runs={"ok": True, "metadata": "service@https://metadata.x/p/default/", "runs": [
        {"id": "argo-fstbackfill.prod.x-1", "finished": True, "successful": True, "created_at": "2026-10-08 10:00:00"}]}))
    result = probe.run_probe()
    stages = _by_id(result)
    assert stages["auth"]["status"] == "pass" and "2 config keys (values not shown)" in stages["auth"]["detail"]
    assert stages["list_runs"]["status"] == "pass" and "argo-fstbackfill.prod.x-1" in stages["list_runs"]["detail"]
    assert result["overall"] == "pass"
    assert TOKEN not in json.dumps(result)
    assert TOKEN not in "\n".join(record.getMessage() for record in caplog.records)


# --- trigger gating ------------------------------------------------------------

def test_trigger_skipped_when_requested_but_flag_off(monkeypatch):
    monkeypatch.setenv("OUTERBOUNDS_PROBE_DEPLOYMENT_ID", "fstbackfill.test.devfstmon.fstbackfill")
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_http_get", _http(403))
    child = Child()
    monkeypatch.setattr(probe, "_run_child", child)
    result = probe.run_probe(include_trigger=True, deployment_id="fstbackfill.test.devfstmon.fstbackfill")
    trigger = _by_id(result)["trigger"]
    assert trigger["status"] == "skip"
    assert "OUTERBOUNDS_PROBE_TRIGGER_ENABLED is not 1" in trigger["detail"]
    assert "trigger" not in child.calls


def test_trigger_requires_all_three_gates(monkeypatch):
    monkeypatch.setenv("OUTERBOUNDS_PROBE_TRIGGER_ENABLED", "1")
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_http_get", _http(403))
    child = Child()
    monkeypatch.setattr(probe, "_run_child", child)
    # Flag on but no include_trigger and no allowlisted deployment -> skip.
    trigger = _by_id(probe.run_probe())["trigger"]
    assert trigger["status"] == "skip"
    assert "include_trigger" in trigger["detail"] and "OUTERBOUNDS_PROBE_DEPLOYMENT_ID" in trigger["detail"]
    assert "trigger" not in child.calls


# --- logging / misc ------------------------------------------------------------------

def test_one_log_line_per_stage_plus_summary(monkeypatch, caplog):
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_http_get", _http(403))
    monkeypatch.setattr(probe, "_run_child", Child())
    probe.run_probe()
    lines = [record.getMessage() for record in caplog.records if record.name == probe.LOGGER_NAME]
    assert all(line.startswith("[outerbounds-probe] ") for line in lines)
    assert sum(" stage " in line for line in lines) == 5
    assert lines[-1].startswith("[outerbounds-probe] probe done: ") and "overall=expected_fail" in lines[-1]
    assert probe.LOGGER_NAME.startswith("uvicorn.error.")


def test_busy_probe_is_rejected(monkeypatch):
    assert probe._RUN_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(probe.ProbeBusy):
            probe.run_probe()
    finally:
        probe._RUN_LOCK.release()


def test_redact_patterns():
    text = probe.redact(f"x-api-key: {TOKEN} Authorization: Bearer abc.def.ghi {CONFIG_URL} token={TOKEN}", (TOKEN,))
    assert TOKEN not in text and "abc.def.ghi" not in text and "abcdefghijklmnop0123" not in text


def test_child_env_drops_unrelated_secrets(monkeypatch):
    monkeypatch.setenv("MONGODB_URL", "mongodb+srv://u:p@h/db")
    monkeypatch.setenv("FST_PROD_ACCOUNT_KEY", "k")
    env = probe._child_env(probe.ProbeConfig.from_env())
    assert "MONGODB_URL" not in env and "FST_PROD_ACCOUNT_KEY" not in env
    assert env["PYTHONPATH"].split(":")[0].endswith("backend")
