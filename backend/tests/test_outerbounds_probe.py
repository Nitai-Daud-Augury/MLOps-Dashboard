"""Outerbounds connectivity probe: gating, staged results, redaction.

No test reaches the network or runs Metaflow: sockets, HTTP and the Metaflow
child process are mocked.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backfill_dashboard import outerbounds_probe as probe
from test_runtime_mode import _request

STAGE_KEYS = {"id", "name", "status", "detail", "error_type", "ms", "description", "http_status", "hint"}
REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "obp-test-token-0123456789abcdef"
CONFIG_URL = "https://api.augury.obp.outerbounds.com/v1/perimeters/default/abcdefghijklmnop0123/default"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path, caplog):
    # Import config first: it runs load_dotenv(.env) once, so a developer's local
    # .env (e.g. OUTERBOUNDS_PROBE_ENABLED=1) cannot re-populate the keys cleared below.
    import backfill_dashboard.config  # noqa: F401

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
    assert [stage["id"] for stage in response.body["stages"]] == ["network", "packages", "auth", "list_runs", "github_dispatch", "trigger"]
    for stage in response.body["stages"]:
        assert set(stage) == STAGE_KEYS
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
    assert sum(" stage " in line for line in lines) == 6
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


# --- child interpreter / cwd independence -------------------------------------------

class FakeRun:
    """Stands in for subprocess.run: records the launch and answers like a child."""

    def __init__(self, result=None):
        self.calls: list[dict] = []
        self.result = result or {"ok": True, "metadata": "local@/tmp", "metaflow_version": "2.19.34.1", "versions": {}}

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        return SimpleNamespace(stdout=probe._CHILD_MARKER + json.dumps(self.result), stderr="", returncode=0)


def _fake_python(directory, name="python", executable=True):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755 if executable else 0o644)
    return path


def _no_launch(command, **kwargs):
    pytest.fail(f"child must not be launched with an unusable interpreter: {command[0]}")


def test_launcher_defaults_to_sys_executable_from_foreign_cwd(monkeypatch, tmp_path):
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)  # no .venv here, unlike the repo root
    fake = FakeRun()
    monkeypatch.setattr(probe.subprocess, "run", fake)

    out = probe._run_child("packages", {"timeout": 5}, probe.ProbeConfig.from_env(), ())

    assert out["ok"] is True
    call = fake.calls[0]
    command = call["command"]
    assert command[0] == os.path.abspath(sys.executable) and os.path.isabs(command[0])
    assert command[1:4] == ["-m", "backfill_dashboard.outerbounds_probe", "--child"]
    assert not any(arg.startswith(("./", "../", ".venv")) for arg in command)
    assert os.path.isabs(call["cwd"]) and call["cwd"] != str(workdir)
    pythonpath = call["env"]["PYTHONPATH"].split(os.pathsep)
    assert pythonpath[0] == str(probe._BACKEND_DIR) and all(os.path.isabs(item) for item in pythonpath)
    assert os.path.isabs(call["env"]["METAFLOW_HOME"])


def test_relative_sys_executable_resolves_against_startup_dir(monkeypatch, tmp_path):
    """Databricks Apps starts the app as `.venv/bin/python`: sys.executable is relative
    to the app root, and the old launcher resolved it against backend/ instead."""
    app_root = tmp_path / "app"
    python = _fake_python(app_root / ".venv" / "bin")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(probe, "_STARTUP_CWD", str(app_root))
    monkeypatch.setattr(probe.sys, "executable", ".venv/bin/python")
    monkeypatch.chdir(other)

    interpreter = probe.resolve_interpreter()

    assert interpreter.ok and interpreter.source == "sys.executable"
    assert interpreter.path == str(python)


def test_real_child_launch_works_from_temp_cwd(monkeypatch, tmp_path):
    """End to end: the child starts and answers from a cwd with no .venv / backend."""
    monkeypatch.chdir(tmp_path)
    out = probe._run_child("not-a-stage", {"timeout": 5}, probe.ProbeConfig.from_env(), ())
    assert out.get("error_type") not in ("interpreter_missing", "child_launch_failed", "ChildProcessError", "TimeoutError"), out
    assert out["ok"] is False  # unknown stage (or no metaflow) is reported by the child itself


def test_valid_override_is_used(monkeypatch, tmp_path):
    python = _fake_python(tmp_path / "custom" / "bin", "python3.11")
    monkeypatch.setenv("OUTERBOUNDS_PROBE_PYTHON", str(python))
    fake = FakeRun()
    monkeypatch.setattr(probe.subprocess, "run", fake)

    interpreter = probe.resolve_interpreter()
    probe._run_child("packages", {"timeout": 5}, probe.ProbeConfig.from_env(), ())

    assert interpreter.ok and interpreter.source == "OUTERBOUNDS_PROBE_PYTHON" and interpreter.path == str(python)
    assert fake.calls[0]["command"][0] == str(python)


def test_relative_override_is_made_absolute(monkeypatch, tmp_path):
    python = _fake_python(tmp_path / "venv" / "bin")
    monkeypatch.setattr(probe, "_STARTUP_CWD", str(tmp_path))
    monkeypatch.setenv("OUTERBOUNDS_PROBE_PYTHON", "venv/bin/python")
    assert probe.resolve_interpreter().path == str(python)


@pytest.mark.parametrize("kind", ["missing", "not_executable", "directory"])
def test_invalid_override_fails_cleanly_and_other_stages_run(monkeypatch, tmp_path, caplog, kind):
    if kind == "missing":
        target = tmp_path / "nope" / "python"
    elif kind == "not_executable":
        target = _fake_python(tmp_path / "bin", executable=False)
    else:
        target = tmp_path / "bin"
        target.mkdir()
    monkeypatch.setenv("OUTERBOUNDS_PROBE_PYTHON", str(target))
    monkeypatch.setattr(probe.subprocess, "run", _no_launch)
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_http_get", _http(403))

    result = probe.run_probe()
    stages = _by_id(result)

    for stage_id in ("packages", "list_runs"):
        assert stages[stage_id]["status"] == "fail"
        assert stages[stage_id]["error_type"] == "interpreter_missing"
        assert str(target) in stages[stage_id]["detail"]
        assert "Traceback" not in stages[stage_id]["detail"] and "probe error" not in stages[stage_id]["detail"]
    assert stages["network"]["status"] == "pass"
    assert stages["auth"]["status"] == "expected_fail"
    assert stages["trigger"]["status"] == "skip"
    assert result["overall"] == "fail"
    assert "Traceback" not in caplog.text
    status = probe.describe_config()["python"]
    assert status["ok"] is False and status["path"] == str(target)


def test_unusable_sys_executable_fails_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "_STARTUP_CWD", str(tmp_path))
    monkeypatch.setattr(probe.sys, "executable", ".venv/bin/python")
    monkeypatch.setattr(probe.sys, "prefix", str(tmp_path / "noprefix"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(probe.subprocess, "run", _no_launch)

    out = probe._run_child("packages", {"timeout": 5}, probe.ProbeConfig.from_env(), ())

    assert out["error_type"] == "interpreter_missing"
    assert str(tmp_path / ".venv" / "bin" / "python") in out["error"]
    assert "OUTERBOUNDS_PROBE_PYTHON" in out["error"]


def test_launch_oserror_is_interpreter_missing_not_traceback(monkeypatch):
    def boom(command, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", command[0])

    monkeypatch.setattr(probe.subprocess, "run", boom)
    out = probe._run_child("list_runs", {"flow": "FSTBackfill", "timeout": 5}, probe.ProbeConfig.from_env(), ())
    assert out["error_type"] == "interpreter_missing"
    assert "No such file or directory" in out["error"]


def test_interpreter_failure_does_not_leak_token(monkeypatch, tmp_path, caplog):
    from backfill_dashboard import app as app_module

    _with_token(monkeypatch, tmp_path)
    monkeypatch.setenv("OUTERBOUNDS_PROBE_ENABLED", "1")
    monkeypatch.setenv("OUTERBOUNDS_PROBE_PYTHON", str(tmp_path / "missing-python"))
    monkeypatch.setattr(probe.subprocess, "run", _no_launch)
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_http_get", _http(200, json_body={"config": {"A": "x"}}))

    response = _request(app_module.app, "POST", "/api/diagnostics/outerbounds", {"flow": "FSTBackfill"})
    status = _request(app_module.app, "GET", "/api/diagnostics/outerbounds/status")

    assert response.status_code == 200
    assert _by_id(response.body)["packages"]["error_type"] == "interpreter_missing"
    text = json.dumps(response.body) + json.dumps(status.body) + caplog.text
    assert TOKEN not in text and "abcdefghijklmnop0123" not in text


def test_relative_metaflow_home_is_absolute_in_child_env(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "_STARTUP_CWD", str(tmp_path))
    monkeypatch.setenv("METAFLOW_HOME", "mfconfig")
    env = probe._child_env(probe.ProbeConfig.from_env())
    assert env["METAFLOW_HOME"] == str(tmp_path / "mfconfig")
    assert probe._metaflow_home() == tmp_path / "mfconfig"


# --- UI contract: fields, descriptions, hints ------------------------------------------

def _ts_interface_fields(name):
    source = (REPO_ROOT / "src" / "components" / "outerbounds-probe" / "probeApi.ts").read_text(encoding="utf-8")
    body = re.search(r"export interface " + name + r" \{(.*?)\n\}", source, re.S).group(1)
    return set(re.findall(r"^\s*([a-z_]+)\??:", body, re.M))


def test_stage_schema_matches_frontend_types(monkeypatch):
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_run_child", Child())
    monkeypatch.setattr(probe, "_http_get", _http(403))
    result = probe.run_probe()
    for stage in result["stages"]:
        assert set(stage) == STAGE_KEYS
    assert _ts_interface_fields("ProbeStage") == STAGE_KEYS
    assert set(result["counts"]) == _ts_interface_fields("ProbeCounts")
    assert {"probe_id", "started_at", "finished_at", "ms", "overall", "flow", "counts", "stages"} <= _ts_interface_fields("ProbeResult")
    assert _ts_interface_fields("ProbeResult") <= set(result)


def test_every_stage_has_a_description():
    assert set(probe.STAGE_DESCRIPTIONS) == {stage_id for stage_id, _ in probe.STAGES}
    assert all(len(text) > 20 for text in probe.STAGE_DESCRIPTIONS.values())


def test_hints_for_the_no_credential_dev_case(monkeypatch):
    """The deployed dev app today: network/packages pass, auth 403, list_runs local MetaflowNotFound, trigger skip."""
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_run_child", Child(list_runs={
        "ok": False, "metadata": "local@/tmp", "error_type": "metaflow.exception.MetaflowNotFound",
        "error": "Flow('FSTBackfill') does not exist"}))
    monkeypatch.setattr(probe, "_http_get", _http(403, "<html>403 Forbidden</html>"))
    stages = _by_id(probe.run_probe())

    assert stages["network"]["hint"].startswith("Outbound DNS, TCP and TLS")
    assert "local metadata" in stages["packages"]["hint"]
    auth = stages["auth"]
    assert auth["status"] == "expected_fail" and auth["error_type"] == "HTTP403" and auth["http_status"] == 403
    assert "No Outerbounds credential configured" in auth["hint"]
    assert "METAFLOW_SERVICE_AUTH_KEY / OBP_METAFLOW_CONFIG_URL" in auth["hint"] and "Outerbounds owner" in auth["hint"]
    runs = stages["list_runs"]
    assert runs["status"] == "expected_fail" and runs["http_status"] is None
    assert "fell back to local metadata" in runs["hint"]
    assert "Nothing was triggered" in stages["trigger"]["hint"]
    assert all(stage["description"] == probe.STAGE_DESCRIPTIONS[stage["id"]] for stage in stages.values())


def test_auth_pass_reports_http_200(monkeypatch, tmp_path):
    _with_token(monkeypatch, tmp_path)
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_run_child", Child())
    monkeypatch.setattr(probe, "_http_get", _http(200, json_body={"config": {}}))
    auth = _by_id(probe.run_probe())["auth"]
    assert auth["status"] == "pass" and auth["http_status"] == 200 and auth["error_type"] is None
    assert auth["hint"] == "The configured Outerbounds credential was accepted."


def test_rejected_credential_hint(monkeypatch, tmp_path):
    _with_token(monkeypatch, tmp_path)
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_run_child", Child())
    monkeypatch.setattr(probe, "_http_get", _http(401, "denied"))
    auth = _by_id(probe.run_probe())["auth"]
    assert auth["status"] == "fail" and auth["http_status"] == 401 and "rejected" in auth["hint"]


def test_dns_failure_hint(monkeypatch):
    def fail(host, timeout):
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(probe, "_check_host", fail)
    monkeypatch.setattr(probe, "_run_child", Child())
    monkeypatch.setattr(probe, "_http_get", _http(403))
    network = _by_id(probe.run_probe())["network"]
    assert network["status"] == "fail" and network["error_type"] == "socket.gaierror"
    assert "DNS" in network["hint"] and "egress" in network["hint"]


def test_interpreter_missing_hint_names_override(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTERBOUNDS_PROBE_PYTHON", str(tmp_path / "missing"))
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_http_get", _http(403))
    stages = _by_id(probe.run_probe())
    for stage_id in ("packages", "list_runs"):
        assert stages[stage_id]["error_type"] == "interpreter_missing"
        assert "OUTERBOUNDS_PROBE_PYTHON" in stages[stage_id]["hint"]


def test_missing_metaflow_hint(monkeypatch):
    _ok_network(monkeypatch)
    monkeypatch.setattr(probe, "_run_child", Child(packages={"ok": False, "error_type": "ModuleNotFoundError", "error": "No module named 'metaflow'"}))
    monkeypatch.setattr(probe, "_http_get", _http(403))
    packages = _by_id(probe.run_probe())["packages"]
    assert packages["status"] == "fail" and "requirements.txt" in packages["hint"]


def test_long_detail_is_never_silently_truncated(monkeypatch):
    _ok_network(monkeypatch)
    long_error = "x" * 1500
    monkeypatch.setattr(probe, "_run_child", Child(list_runs={"ok": False, "metadata": "local@/x", "error_type": "RuntimeError", "error": long_error}))
    monkeypatch.setattr(probe, "_http_get", _http(403))
    runs = _by_id(probe.run_probe())["list_runs"]
    assert long_error in runs["detail"]  # 1500 chars fit: kept in full
    assert "truncated" not in runs["detail"]
    clipped = probe._clip("y" * (probe.DETAIL_LIMIT + 25), probe.DETAIL_LIMIT)
    assert clipped.endswith("[+25 chars truncated]")

