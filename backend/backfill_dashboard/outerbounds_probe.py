"""Read-only Outerbounds connectivity probe (diagnostics only).

Answers "can this dashboard (locally or inside Databricks Apps) reach and
authenticate to Outerbounds, and list runs of an allowlisted flow?" with a
small staged report. Every stage returns::

    {"id", "name", "status": pass|fail|skip|expected_fail, "detail", "error_type", "ms"}

Stages, in order (each capped by OUTERBOUNDS_PROBE_STAGE_TIMEOUT_SECONDS,
default 20s, clamped to 5..30s):

1. network   - DNS + TCP + TLS to the deployment's service hosts on :443
               (``api.<domain>`` and ``metadata.<domain>``; the bare domain has
               no DNS record) plus OUTERBOUNDS_PROBE_EXTRA_HOST when set.
2. packages  - import metaflow (ob-metaflow + ob-metaflow-extensions) in a child
               process, record versions and which config keys are present
               (names only).
3. auth      - with credentials: fetch the Outerbounds remote config
               (``OBP_METAFLOW_CONFIG_URL`` with ``x-api-key``), the same call
               Metaflow makes first; without credentials: unauthenticated GET of
               the metadata service ping, where 401/403 is ``expected_fail``.
4. list_runs - Metaflow client ``Flow(<flow>).runs()`` (read-only, first 3) for
               a flow in OUTERBOUNDS_PROBE_FLOW_ALLOWLIST. Always attempted; the
               exact exception type and (redacted) message are recorded.
5. trigger   - skipped unless OUTERBOUNDS_PROBE_TRIGGER_ENABLED=1 *and* the
               request sets include_trigger *and* OUTERBOUNDS_PROBE_DEPLOYMENT_ID
               is configured and matches the request.

Metaflow work runs in a short-lived child process: it keeps the API process
free of Metaflow's global config (the Outerbounds extension caches the resolved
config, including the service key, in ``os.environ``), lets a hung call be
killed at the stage deadline, and makes "no config" runs reproducible.

Secrets (service keys, Authorization / x-api-key headers, the config URL path)
are never returned or logged; all text passes through ``redact``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

LOGGER_NAME = "uvicorn.error.outerbounds_probe"
PREFIX = "[outerbounds-probe]"
logger = logging.getLogger(LOGGER_NAME)

DEFAULT_DOMAIN = "augury.obp.outerbounds.com"
DEFAULT_PERIMETER = "default"
DEFAULT_FLOW_ALLOWLIST = ("FSTBackfill",)
DEFAULT_STAGE_TIMEOUT_SECONDS = 20.0
_MIN_STAGE_TIMEOUT, _MAX_STAGE_TIMEOUT = 5.0, 30.0
_TRUE = {"1", "true", "yes", "on"}
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")

# Config keys whose *presence* is reported (never their values).
CONFIG_ENV_KEYS = (
    "METAFLOW_HOME",
    "METAFLOW_PROFILE",
    "METAFLOW_SERVICE_URL",
    "METAFLOW_SERVICE_AUTH_KEY",
    "METAFLOW_SERVICE_HEADERS",
    "METAFLOW_DEFAULT_METADATA",
    "OBP_METAFLOW_CONFIG_URL",
    "OBP_API_SERVER",
    "OBP_PERIMETER",
    "OUTERBOUNDS_DEPLOYMENT_DOMAIN",
    "OUTERBOUNDS_PERIMETER",
    "OUTERBOUNDS_PROBE_FLOW_ALLOWLIST",
    "OUTERBOUNDS_PROBE_DEPLOYMENT_ID",
    "OUTERBOUNDS_PROBE_EXTRA_HOST",
)
PACKAGE_NAMES = ("ob-metaflow", "ob-metaflow-extensions", "metaflow", "outerbounds")
STAGES = (
    ("network", "Network: DNS + TCP + TLS"),
    ("packages", "Packages: metaflow import"),
    ("auth", "Auth: Outerbounds credentials"),
    ("list_runs", "List runs (read-only)"),
    ("trigger", "Trigger deployment"),
)


class ProbeNotAllowed(ValueError):
    """Request names a flow/deployment that is not allowlisted (HTTP 422)."""


class ProbeBusy(RuntimeError):
    """Another probe is already running (HTTP 409)."""


# --- redaction / logging ---------------------------------------------------------

_REDACTIONS = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)(x-api-key|authorization|api[_-]?key|auth[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)([^\s\"',;}&]+)"), r"\1\2<redacted>"),
    (re.compile(r"(?i)mongodb(?:\+srv)?://\S+"), "mongodb://<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"), "<redacted-jwt>"),
    (re.compile(r"(?i)(/v1/perimeters/[^/\s]+/)[^/\s\"']+"), r"\1<redacted>"),
    (re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"), "https://<redacted>@"),
)


def redact(text: Any, secrets: tuple[str, ...] | list[str] = ()) -> str:
    value = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            value = value.replace(secret, "<redacted>")
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def _log(level: int, message: str, secrets: tuple[str, ...] = ()) -> None:
    logger.log(level, "%s %s", PREFIX, redact(message, secrets))


# --- configuration ---------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def probe_enabled() -> bool:
    return _env("OUTERBOUNDS_PROBE_ENABLED").lower() in _TRUE


def trigger_enabled() -> bool:
    return _env("OUTERBOUNDS_PROBE_TRIGGER_ENABLED").lower() in _TRUE


def _stage_timeout() -> float:
    try:
        value = float(_env("OUTERBOUNDS_PROBE_STAGE_TIMEOUT_SECONDS") or DEFAULT_STAGE_TIMEOUT_SECONDS)
    except ValueError:
        value = DEFAULT_STAGE_TIMEOUT_SECONDS
    return min(_MAX_STAGE_TIMEOUT, max(_MIN_STAGE_TIMEOUT, value))


def flow_allowlist() -> tuple[str, ...]:
    raw = _env("OUTERBOUNDS_PROBE_FLOW_ALLOWLIST")
    names = [item.strip() for item in raw.split(",") if item.strip()] if raw else list(DEFAULT_FLOW_ALLOWLIST)
    return tuple(name for name in names if _NAME_RE.match(name))


@dataclass(frozen=True)
class Credentials:
    source: str  # "env" | "config_file" | "none"
    token: str = field(default="", repr=False)
    config_url: str = field(default="", repr=False)
    service_url: str = ""
    config_path: str = ""

    @property
    def present(self) -> bool:
        return bool(self.token)


def _metaflow_home() -> Path:
    return Path(os.environ.get("METAFLOW_HOME") or (Path.home() / ".metaflowconfig"))


def resolve_credentials() -> Credentials:
    """Find Outerbounds credentials without exposing them (env first, then config file)."""
    token = _env("METAFLOW_SERVICE_AUTH_KEY")
    if token:
        return Credentials("env", token, _env("OBP_METAFLOW_CONFIG_URL"), _env("METAFLOW_SERVICE_URL"))
    profile = _env("METAFLOW_PROFILE")
    path = _metaflow_home() / (f"config_{profile}.json" if profile else "config.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Credentials("none")
    if not isinstance(data, dict):
        return Credentials("none")
    token = str(data.get("METAFLOW_SERVICE_AUTH_KEY") or "")
    config_url = str(data.get("OBP_METAFLOW_CONFIG_URL") or "")
    try:
        ob = json.loads((path.parent / "ob_config.json").read_text(encoding="utf-8"))
        config_url = str(ob.get("OB_CURRENT_PERIMETER_MF_CONFIG_URL") or config_url)
    except (OSError, ValueError, AttributeError):
        pass
    if not token:
        return Credentials("none", config_path=str(path))
    return Credentials("config_file", token, config_url, str(data.get("METAFLOW_SERVICE_URL") or ""), str(path))


@dataclass(frozen=True)
class ProbeConfig:
    domain: str
    perimeter: str
    flows: tuple[str, ...]
    deployment_id: str
    extra_host: str
    stage_timeout: float
    trigger_enabled: bool

    @classmethod
    def from_env(cls) -> "ProbeConfig":
        domain = _env("OUTERBOUNDS_DEPLOYMENT_DOMAIN", DEFAULT_DOMAIN).lower()
        if not _HOST_RE.match(domain):
            domain = DEFAULT_DOMAIN
        extra = _env("OUTERBOUNDS_PROBE_EXTRA_HOST").lower()
        perimeter = _env("OUTERBOUNDS_PERIMETER", DEFAULT_PERIMETER)
        return cls(
            domain=domain,
            perimeter=perimeter if _NAME_RE.match(perimeter) else DEFAULT_PERIMETER,
            flows=flow_allowlist(),
            deployment_id=_env("OUTERBOUNDS_PROBE_DEPLOYMENT_ID"),
            extra_host=extra if _HOST_RE.match(extra) else "",
            stage_timeout=_stage_timeout(),
            trigger_enabled=trigger_enabled(),
        )

    @property
    def hosts(self) -> list[str]:
        hosts = [f"api.{self.domain}", f"metadata.{self.domain}"]
        if self.extra_host and self.extra_host not in hosts:
            hosts.append(self.extra_host)
        return hosts

    @property
    def metadata_ping_url(self) -> str:
        return f"https://metadata.{self.domain}/p/{self.perimeter}/ping"


def config_keys_presence() -> dict[str, list[str]]:
    present = [key for key in CONFIG_ENV_KEYS if os.environ.get(key)]
    absent = [key for key in CONFIG_ENV_KEYS if not os.environ.get(key)]
    return {"present": present, "absent": absent}


def describe_config() -> dict[str, Any]:
    """Non-secret view for the status endpoint and UI."""
    cfg = ProbeConfig.from_env()
    creds = resolve_credentials()
    return {
        "enabled": probe_enabled(),
        "trigger_enabled": cfg.trigger_enabled,
        "domain": cfg.domain,
        "perimeter": cfg.perimeter,
        "hosts": cfg.hosts,
        "flow_allowlist": list(cfg.flows),
        "deployment_id_configured": bool(cfg.deployment_id),
        "stage_timeout_seconds": cfg.stage_timeout,
        "credentials_source": creds.source,
        "config_keys": config_keys_presence(),
    }


def validate_request(flow: str | None, include_trigger: bool, deployment_id: str | None, cfg: ProbeConfig) -> str:
    """Return the flow to probe; raise ProbeNotAllowed for anything not allowlisted."""
    if not cfg.flows:
        raise ProbeNotAllowed("OUTERBOUNDS_PROBE_FLOW_ALLOWLIST is empty")
    selected = (flow or cfg.flows[0]).strip()
    if selected not in cfg.flows:
        raise ProbeNotAllowed(f"flow {selected!r} is not in OUTERBOUNDS_PROBE_FLOW_ALLOWLIST ({', '.join(cfg.flows)})")
    if deployment_id is not None and deployment_id.strip():
        if not cfg.deployment_id or deployment_id.strip() != cfg.deployment_id:
            raise ProbeNotAllowed("deployment_id is not the allowlisted OUTERBOUNDS_PROBE_DEPLOYMENT_ID")
    if not isinstance(include_trigger, bool):
        raise ProbeNotAllowed("include_trigger must be a boolean")
    return selected


# --- stage helpers -----------------------------------------------------------------

def _stage(stage_id: str, status: str, detail: str, error_type: str | None, started: float) -> dict[str, Any]:
    name = dict(STAGES)[stage_id]
    return {
        "id": stage_id,
        "name": name,
        "status": status,
        "detail": detail,
        "error_type": error_type,
        "ms": int((time.monotonic() - started) * 1000),
    }


def _with_deadline(func: Callable[[], Any], seconds: float) -> Any:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - resurfaced below
            box["error"] = exc

    thread = threading.Thread(target=run, name="ob-probe", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"timed out after {seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _check_host(host: str, timeout: float) -> str:
    addresses = _with_deadline(lambda: socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP), timeout)
    ips = sorted({item[4][0] for item in addresses})
    context = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            version = tls.version()
            cert = tls.getpeercert() or {}
    days = ""
    if cert.get("notAfter"):
        try:
            expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
            days = f", cert expires in {(expires - datetime.now(timezone.utc)).days}d"
        except (ValueError, OverflowError):
            pass
    return f"{host}: dns {len(ips)} addr(s), tcp ok, tls {version}{days}"


# --- child process (Metaflow client) ------------------------------------------------

_CHILD_MARKER = "__OB_PROBE_RESULT__"
_UNRELATED_SECRET_RE = re.compile(r"^(MONGODB_|FST_PROD_ACCOUNT_KEY|AZURE_CLIENT_SECRET|DATABRICKS_CLIENT_SECRET|DATABRICKS_TOKEN)")


def _child_env(cfg: ProbeConfig) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("__REMOTE_CONFIG_HAS_BEEN_RESOLVED__", None)
    # Least privilege: the child only needs Metaflow/Outerbounds settings.
    for key in list(env):
        if _UNRELATED_SECRET_RE.search(key):
            env.pop(key, None)
    backend = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = backend + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def _run_child(stage: str, payload: dict[str, Any], cfg: ProbeConfig, secrets: tuple[str, ...]) -> dict[str, Any]:
    """Run one Metaflow stage in a child interpreter with a hard deadline."""
    command = [sys.executable, "-m", "backfill_dashboard.outerbounds_probe", "--child", json.dumps({"stage": stage, **payload})]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=cfg.stage_timeout,
            env=_child_env(cfg),
            cwd=str(Path(__file__).resolve().parents[1]),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_type": "TimeoutError", "error": f"stage exceeded {cfg.stage_timeout:g}s (child killed)"}
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_CHILD_MARKER):
            try:
                return json.loads(line[len(_CHILD_MARKER):])
            except ValueError:
                break
    tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-3:]
    return {
        "ok": False,
        "error_type": "ChildProcessError",
        "error": redact(f"exit {completed.returncode}: {' | '.join(tail)}", secrets)[:400],
    }


def _exc_type(exc: BaseException) -> str:
    module = type(exc).__module__
    return type(exc).__name__ if module in ("builtins", "__main__") else f"{module}.{type(exc).__name__}"


def _bootstrap_remote_config_from_env(timeout: float) -> str:
    """Env-only credentials (no config file): resolve the Outerbounds remote
    config like the extension does, into this child's environment only."""
    url, key = os.environ.get("OBP_METAFLOW_CONFIG_URL"), os.environ.get("METAFLOW_SERVICE_AUTH_KEY")
    home = _metaflow_home()
    if not url or not key or (home / "config.json").exists():
        return ""
    import requests

    response = requests.get(url, headers={"x-api-key": key}, timeout=timeout)
    response.raise_for_status()
    config = response.json().get("config") or {}
    for name, value in config.items():
        os.environ[str(name)] = value if isinstance(value, str) else json.dumps(value)
    return f"remote config resolved from env ({len(config)} keys)"


def child_main(request: dict[str, Any]) -> dict[str, Any]:
    """Executed in the child process. Returns a JSON-able dict; never secrets."""
    stage = request.get("stage")
    timeout = float(request.get("timeout") or 10)
    result: dict[str, Any] = {"ok": False}
    try:
        note = _bootstrap_remote_config_from_env(timeout)
        started = time.monotonic()
        import metaflow  # noqa: F401 - the import itself is the check (resolves OB config)

        result["import_s"] = round(time.monotonic() - started, 2)
        result["metaflow_version"] = getattr(metaflow, "__version__", "?")
        if note:
            result["note"] = note
        from metaflow import get_metadata

        result["metadata"] = str(get_metadata())
        if stage == "packages":
            versions = {}
            try:
                import importlib.metadata as md

                for name in PACKAGE_NAMES:
                    try:
                        versions[name] = md.version(name)
                    except md.PackageNotFoundError:
                        versions[name] = None
            except Exception:  # noqa: BLE001
                pass
            result.update(ok=True, versions=versions)
        elif stage == "list_runs":
            import itertools

            from metaflow import Flow, namespace

            namespace(None)
            runs = list(itertools.islice(Flow(str(request["flow"])).runs(), 3))
            result.update(ok=True, runs=[
                {"id": run.id, "finished": bool(run.finished), "successful": bool(run.successful), "created_at": str(run.created_at)[:19]}
                for run in runs
            ])
        elif stage == "trigger":
            from metaflow import DeployedFlow

            deployed = DeployedFlow.from_deployment(str(request["deployment_id"]))
            triggered = deployed.trigger()
            result.update(ok=True, triggered=str(getattr(triggered, "pathspec", "") or getattr(triggered, "name", "")))
        else:
            result.update(error_type="ValueError", error=f"unknown stage {stage!r}")
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        result.update(ok=False, error_type=_exc_type(exc), error=str(exc)[:500])
    return result


# --- stages --------------------------------------------------------------------------

def stage_network(cfg: ProbeConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    per_host = max(2.0, min(8.0, cfg.stage_timeout / max(1, len(cfg.hosts))))
    lines, first_error = [], None
    for host in cfg.hosts:
        try:
            lines.append(_check_host(host, per_host))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{host}: {_exc_type(exc)}: {exc}")
            first_error = first_error or _exc_type(exc)
    ctx["network_ok"] = first_error is None
    status = "pass" if first_error is None else "fail"
    return _stage("network", status, "; ".join(lines), first_error, started)


def stage_packages(cfg: ProbeConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    keys = config_keys_presence()
    creds = ctx["credentials"]
    keys_text = f"config keys present: {', '.join(keys['present']) or 'none'}; credentials: {creds.source}"
    out = _run_child("packages", {"timeout": cfg.stage_timeout}, cfg, ctx["secrets"])
    ctx["metaflow_ok"] = bool(out.get("ok"))
    if not out.get("ok"):
        return _stage("packages", "fail", f"metaflow import failed: {out.get('error', '')}; {keys_text}", out.get("error_type"), started)
    versions = ", ".join(f"{name}={version}" for name, version in (out.get("versions") or {}).items() if version)
    provider = str(out.get("metadata", "")).split("@")[0] or "?"
    detail = f"metaflow {out.get('metaflow_version')} imported in {out.get('import_s')}s ({versions}); metadata provider: {provider}; {keys_text}"
    return _stage("packages", "pass", detail, None, started)


def _http_get(url: str, headers: dict[str, str], timeout: float):
    import requests

    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)


def stage_auth(cfg: ProbeConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    creds: Credentials = ctx["credentials"]
    timeout = min(cfg.stage_timeout, 15.0)
    try:
        if creds.present and creds.config_url:
            target = f"remote config at {urlparse(creds.config_url).hostname}"
            response = _http_get(creds.config_url, {"x-api-key": creds.token}, timeout)
        elif creds.present and creds.service_url:
            target = f"metadata ping at {urlparse(creds.service_url).hostname}"
            response = _http_get(creds.service_url.rstrip("/") + "/ping", {"x-api-key": creds.token}, timeout)
        else:
            target = f"unauthenticated metadata ping at metadata.{cfg.domain}"
            response = _http_get(cfg.metadata_ping_url, {}, timeout)
    except Exception as exc:  # noqa: BLE001
        status = "fail"
        return _stage("auth", status, f"request failed ({'credentials: ' + creds.source}): {exc}", _exc_type(exc), started)
    code = response.status_code
    snippet = re.sub(r"\s+", " ", (response.text or "")[:160])
    if not creds.present:
        if code in (401, 403):
            return _stage("auth", "expected_fail", f"no Outerbounds credentials configured; {target} answered HTTP {code} as expected", f"HTTP{code}", started)
        if code < 400:
            return _stage("auth", "pass", f"{target} answered HTTP {code} without credentials", None, started)
        return _stage("auth", "fail", f"no credentials; {target} answered HTTP {code}: {snippet}", f"HTTP{code}", started)
    if code < 400:
        keys = ""
        try:
            keys = f"; {len((response.json() or {}).get('config') or {})} config keys (values not shown)"
        except ValueError:
            pass
        return _stage("auth", "pass", f"credentials ({creds.source}) accepted by {target}: HTTP {code}{keys}", None, started)
    return _stage("auth", "fail", f"credentials ({creds.source}) rejected by {target}: HTTP {code}: {snippet}", f"HTTP{code}", started)


def stage_list_runs(cfg: ProbeConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    flow = ctx["flow"]
    out = _run_child("list_runs", {"flow": flow, "timeout": cfg.stage_timeout}, cfg, ctx["secrets"])
    provider = str(out.get("metadata", "")).split("@")[0] or "unknown"
    if out.get("ok"):
        runs = out.get("runs") or []
        listed = ", ".join(f"{item['id']} ({'done' if item['finished'] else 'running'}, {item['created_at']})" for item in runs) or "no runs"
        return _stage("list_runs", "pass", f"Flow('{flow}').runs() via {provider} metadata: {len(runs)} latest: {listed}", None, started)
    expected = not ctx["credentials"].present
    detail = f"Flow('{flow}').runs() via {provider} metadata raised {out.get('error_type')}: {out.get('error', '')}"
    if expected:
        detail += " (expected: no Outerbounds credentials configured)"
    return _stage("list_runs", "expected_fail" if expected else "fail", detail, out.get("error_type"), started)


def stage_trigger(cfg: ProbeConfig, ctx: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    reasons = []
    if not cfg.trigger_enabled:
        reasons.append("OUTERBOUNDS_PROBE_TRIGGER_ENABLED is not 1")
    if not ctx.get("include_trigger"):
        reasons.append("request did not set include_trigger")
    if not cfg.deployment_id:
        reasons.append("OUTERBOUNDS_PROBE_DEPLOYMENT_ID is not configured")
    elif ctx.get("deployment_id") and ctx["deployment_id"] != cfg.deployment_id:
        reasons.append("deployment_id is not allowlisted")
    if reasons:
        return _stage("trigger", "skip", "skipped: " + "; ".join(reasons), None, started)
    out = _run_child("trigger", {"deployment_id": cfg.deployment_id, "timeout": cfg.stage_timeout}, cfg, ctx["secrets"])
    if out.get("ok"):
        return _stage("trigger", "pass", f"triggered allowlisted deployment: {out.get('triggered')}", None, started)
    return _stage("trigger", "fail", f"trigger failed: {out.get('error', '')}", out.get("error_type"), started)


STAGE_FUNCS = {
    "network": stage_network,
    "packages": stage_packages,
    "auth": stage_auth,
    "list_runs": stage_list_runs,
    "trigger": stage_trigger,
}

# --- runner ----------------------------------------------------------------------------

_RUN_LOCK = threading.Lock()
_LAST_RESULT: dict[str, Any] | None = None


def last_result() -> dict[str, Any] | None:
    return _LAST_RESULT


def _overall(stages: list[dict[str, Any]]) -> str:
    statuses = {stage["status"] for stage in stages}
    if "fail" in statuses:
        return "fail"
    if "expected_fail" in statuses:
        return "expected_fail"
    return "pass"


def run_probe(flow: str | None = None, include_trigger: bool = False, deployment_id: str | None = None) -> dict[str, Any]:
    """Run all stages in order. Raises ProbeNotAllowed (422) / ProbeBusy (409)."""
    global _LAST_RESULT
    cfg = ProbeConfig.from_env()
    selected = validate_request(flow, include_trigger, deployment_id, cfg)
    if not _RUN_LOCK.acquire(blocking=False):
        raise ProbeBusy("an Outerbounds probe is already running")
    try:
        creds = resolve_credentials()
        secrets = tuple(item for item in (creds.token, creds.config_url) if item)
        ctx: dict[str, Any] = {
            "credentials": creds,
            "secrets": secrets,
            "flow": selected,
            "include_trigger": bool(include_trigger),
            "deployment_id": (deployment_id or "").strip(),
        }
        probe_id = uuid.uuid4().hex[:12]
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        _log(logging.INFO, f"probe start: probe_id={probe_id} domain={cfg.domain} perimeter={cfg.perimeter} flow={selected} "
             f"credentials={creds.source} trigger_enabled={cfg.trigger_enabled} include_trigger={bool(include_trigger)}")
        stages = []
        for index, (stage_id, _name) in enumerate(STAGES, start=1):
            stage_started = time.monotonic()
            try:
                result = STAGE_FUNCS[stage_id](cfg, ctx)
            except Exception as exc:  # noqa: BLE001 - a stage bug must not abort the probe
                result = _stage(stage_id, "fail", f"probe error: {exc}", _exc_type(exc), stage_started)
            result["detail"] = redact(result["detail"], secrets)[:900]
            stages.append(result)
            level = logging.WARNING if result["status"] == "fail" else logging.INFO
            _log(level, f"stage {index} {stage_id}: {result['status']} ({result['ms']} ms)"
                 f"{' error_type=' + str(result['error_type']) if result['error_type'] else ''} - {result['detail']}", secrets)
        counts = {status: sum(1 for stage in stages if stage["status"] == status) for status in ("pass", "fail", "expected_fail", "skip")}
        overall = _overall(stages)
        total_ms = int((time.monotonic() - started) * 1000)
        _log(logging.INFO, f"probe done: probe_id={probe_id} overall={overall} pass={counts['pass']} fail={counts['fail']} "
             f"expected_fail={counts['expected_fail']} skip={counts['skip']} total={total_ms} ms", secrets)
        result = {
            "probe_id": probe_id,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "ms": total_ms,
            "overall": overall,
            "counts": counts,
            "flow": selected,
            "config": describe_config(),
            "stages": stages,
        }
        _LAST_RESULT = result
        return result
    finally:
        _RUN_LOCK.release()


def format_table(result: dict[str, Any]) -> str:
    rows = [f"{'#':<2} {'stage':<10} {'status':<14} {'ms':>6}  error_type / detail"]
    for index, stage in enumerate(result["stages"], start=1):
        rows.append(f"{index:<2} {stage['id']:<10} {stage['status']:<14} {stage['ms']:>6}  {stage['error_type'] or '-'} | {stage['detail']}")
    rows.append(f"overall={result['overall']} total={result['ms']} ms")
    return "\n".join(rows)


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "--child":
        result = child_main(json.loads(argv[1]))
        secrets = tuple(item for item in (os.environ.get("METAFLOW_SERVICE_AUTH_KEY", ""),) if item)
        safe = json.loads(redact(json.dumps(result), secrets))
        print(_CHILD_MARKER + json.dumps(safe), flush=True)
        return 0
    # Manual diagnostics: python -m backfill_dashboard.outerbounds_probe [--flow NAME]
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    flow = argv[argv.index("--flow") + 1] if "--flow" in argv else None
    print(format_table(run_probe(flow=flow)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
