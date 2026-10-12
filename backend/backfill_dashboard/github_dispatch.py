"""GitHub Actions dispatch client for Outerbounds actions (stdlib only).

The Databricks App cannot hold an Outerbounds credential, so Outerbounds actions
run in `.github/workflows/outerbounds-dispatch.yml`, which logs in as the OIDC
machine user. This module:

1. finds an existing run for the request (run-name ``ob-<action>-<request_id>``) or
   POSTs ``workflow_dispatch`` (``request_id`` is the idempotency key, so a retried
   call never dispatches twice),
2. finds the run by its run-name (``event=workflow_dispatch``, ``created>=``),
3. polls it until ``completed``,
4. downloads and unzips the ``ob-result-<request_id>`` artifact (``result.json``).

It imports nothing heavy (no FastAPI, no Metaflow) so the Outerbounds probe can use
it from any venv, and it doubles as the admin-action child process:
``python -m backfill_dashboard.github_dispatch '<json>'`` prints ``Name: <workflow>``
for triggers plus one ``__OB_ACTION_RESULT__{json}`` line, like outerbounds_actions.

The GitHub token (``GITHUB_DISPATCH_TOKEN``) is sent only in the Authorization
header to api.github.com and never appears in logs, results or errors; artifact
redirects are followed WITHOUT the header.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

ACTION_MARKER = "__OB_ACTION_RESULT__"
CHILD_MODULE = "backfill_dashboard.github_dispatch"
API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
ACTIONS = ("trigger", "status", "terminate", "list_runs")
TOKEN_ENV = "GITHUB_DISPATCH_TOKEN"
REPO_ENV, REF_ENV, WORKFLOW_ENV = "GITHUB_DISPATCH_REPO", "GITHUB_DISPATCH_REF", "GITHUB_DISPATCH_WORKFLOW"
DEFAULT_REPO = "Nitai-Daud-Augury/MLOps-Dashboard"
DEFAULT_REF = "main"
DEFAULT_WORKFLOW = "outerbounds-dispatch.yml"
ACTION_ID_ENV = "DASHBOARD_ADMIN_ACTION_ID"
NOT_CONFIGURED = "github_dispatch_not_configured"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_REF_RE = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")
_WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}\.ya?ml$")
# https only; plain http just for a loopback test double (the token never leaves TLS otherwise).
_API_URL_RE = re.compile(r"^(https://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._/-]*)?|http://127\.0\.0\.1:\d+)$")
API_URL_ENV = "GITHUB_DISPATCH_API_URL"


def _safe_location(url: str) -> bool:
    return url.startswith("https://") or bool(re.match(r"^http://127\.0\.0\.1:\d+/", url))
MAX_INPUTS_CHARS = 60_000  # GitHub caps the whole workflow_dispatch inputs payload at 65,535 chars
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
FIND_MAX_PAGES = 5


# --- errors -----------------------------------------------------------------------------

class GitHubDispatchError(RuntimeError):
    status_code = 502
    error_type = "github_dispatch_failed"

    def __init__(self, message: str, *, status_code: int | None = None, error_type: str | None = None,
                 run_url: str | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if error_type is not None:
            self.error_type = error_type
        self.run_url = run_url


class GitHubDispatchNotConfigured(GitHubDispatchError):
    status_code = 503
    error_type = NOT_CONFIGURED


class GitHubDispatchTimeout(GitHubDispatchError):
    status_code = 504
    error_type = "github_dispatch_timeout"


# --- configuration ----------------------------------------------------------------------

def _values(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _int_env(name: str, default: int, low: int, high: int, environ: Mapping[str, str] | None = None) -> int:
    try:
        value = int(str(_values(environ).get(name) or default))
    except ValueError:
        value = default
    return max(low, min(high, value))


def token_configured(environ: Mapping[str, str] | None = None) -> bool:
    return bool(str(_values(environ).get(TOKEN_ENV) or "").strip())


def not_configured_message(environ: Mapping[str, str] | None = None) -> str:
    return (
        f"GitHub Actions dispatch not configured: set {TOKEN_ENV} (fine-grained PAT for "
        f"{_values(environ).get(REPO_ENV) or DEFAULT_REPO} with Actions read/write and Contents read) from the "
        "Databricks secret mlops-dashboard/github_dispatch_token. See docs/DEPLOYMENT_RUNBOOK.md, "
        "'Outerbounds via GitHub Actions'."
    )


@dataclass(frozen=True)
class DispatchConfig:
    repo: str
    ref: str
    workflow: str
    token: str = ""
    api_url: str = API_URL

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, require_token: bool = True) -> "DispatchConfig":
        values = _values(environ)
        repo = str(values.get(REPO_ENV) or DEFAULT_REPO).strip()
        ref = str(values.get(REF_ENV) or DEFAULT_REF).strip()
        workflow = str(values.get(WORKFLOW_ENV) or DEFAULT_WORKFLOW).strip()
        token = str(values.get(TOKEN_ENV) or "").strip()
        api_url = str(values.get(API_URL_ENV) or API_URL).strip().rstrip("/")
        if not _API_URL_RE.fullmatch(api_url):
            raise GitHubDispatchNotConfigured(f"{API_URL_ENV} must be an https URL")
        if not _REPO_RE.fullmatch(repo):
            raise GitHubDispatchNotConfigured(f"{REPO_ENV} must look like owner/repo")
        if not _REF_RE.fullmatch(ref) or ".." in ref:
            raise GitHubDispatchNotConfigured(f"{REF_ENV} is not a valid git ref")
        if not _WORKFLOW_RE.fullmatch(workflow):
            raise GitHubDispatchNotConfigured(f"{WORKFLOW_ENV} must be a workflow file name such as {DEFAULT_WORKFLOW}")
        if require_token and not token:
            raise GitHubDispatchNotConfigured(not_configured_message(values))
        return cls(repo=repo, ref=ref, workflow=workflow, token=token, api_url=api_url)

    @property
    def workflow_url(self) -> str:
        return f"https://github.com/{self.repo}/actions/workflows/{self.workflow}"


def redact(text: Any, secrets: tuple[str, ...] = ()) -> str:
    value = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            value = value.replace(secret, "<redacted>")
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]+", "Bearer <redacted>", value)
    value = re.sub(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "<redacted-github-token>", value)
    value = re.sub(r"(?i)(token|authorization|x-api-key|secret)(\"?\s*[:=]\s*\"?)([^\s\"',;}&]+)", r"\1\2<redacted>", value)
    return value


def request_id_for(op: str, payload: Mapping[str, Any], salt: str = "") -> str:
    """Deterministic idempotency key: ``<op>-<24 hex>`` of (pending action id, op, payload).

    The payload carries machine ids, month windows / manifest plan and the deployment,
    so the same pending action always maps to the same GitHub run, and a different
    action (new id) or different windows never collide.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{salt}|{op}|{canonical}".encode()).hexdigest()[:24]
    return f"{op.replace('_', '')[:10]}-{digest}"


def run_name(action: str, request_id: str) -> str:
    return f"ob-{action}-{request_id}"


# --- HTTP -------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - never follow with auth
        return None


Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, Mapping[str, str], bytes]]


def _urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float):
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read(MAX_ARTIFACT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(64 * 1024) if exc.fp else b""


class GitHubClient:
    """Minimal GitHub REST client for one repository/workflow."""

    def __init__(self, config: DispatchConfig, *, transport: Transport | None = None, timeout: float = 20.0) -> None:
        self.config = config
        self._transport = transport or _urllib_transport
        self._timeout = timeout

    def _headers(self, auth: bool = True) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": "mlops-dashboard-outerbounds-dispatch"}
        if auth:
            headers["Authorization"] = f"Bearer {self.config.token}"
        return headers

    def _error(self, method: str, path: str, status: int, body: bytes) -> GitHubDispatchError:
        try:
            message = str((json.loads(body or b"{}") or {}).get("message") or "")
        except ValueError:
            message = ""
        hint = ""
        if status == 401:
            hint = f" The token is invalid or expired: rotate {TOKEN_ENV} (Databricks secret mlops-dashboard/github_dispatch_token)."
        elif status in (403, 404):
            hint = (f" The token cannot see or use {self.config.repo}: a fine-grained PAT needs this repository selected "
                    "with Actions read/write and Contents read (404 is GitHub's answer for no access).")
        elif status == 422 and "dispatches" in path:
            hint = f" Check that {self.config.workflow} exists on ref {self.config.ref} and accepts these inputs."
        return GitHubDispatchError(
            redact(f"GitHub API {method} {path} -> HTTP {status}{': ' + message if message else ''}.{hint}", (self.config.token,)),
            status_code=502, error_type=f"github_http_{status}")

    def request(self, method: str, path: str, *, body: Mapping[str, Any] | None = None,
                expect: tuple[int, ...] = (200,)) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers()
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, _, raw = self._transport(method, self.config.api_url + path, headers, data, self._timeout)
        except (OSError, TimeoutError) as exc:
            raise GitHubDispatchError(redact(f"GitHub API {method} {path} failed: {type(exc).__name__}: {exc}",
                                             (self.config.token,)), status_code=502, error_type="github_unreachable") from exc
        if status not in expect:
            raise self._error(method, path, status, raw)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw

    # repository / workflow -------------------------------------------------------------
    def repo_path(self, suffix: str = "") -> str:
        return f"/repos/{self.config.repo}{suffix}"

    def get_repo(self) -> dict:
        return self.request("GET", self.repo_path())

    def get_workflow(self) -> dict:
        return self.request("GET", self.repo_path(f"/actions/workflows/{self.config.workflow}"))

    def get_workflow_file(self) -> str:
        path = self.repo_path(f"/contents/.github/workflows/{self.config.workflow}?ref={urllib.parse.quote(self.config.ref, safe='')}")
        payload = self.request("GET", path)
        import base64

        return base64.b64decode(str((payload or {}).get("content") or "")).decode("utf-8", "replace")

    # runs ------------------------------------------------------------------------------
    def dispatch(self, inputs: Mapping[str, str]) -> None:
        size = sum(len(str(value)) for value in inputs.values())
        if size > MAX_INPUTS_CHARS:
            raise GitHubDispatchError(
                f"Request too large for GitHub workflow_dispatch ({size} chars of inputs, max {MAX_INPUTS_CHARS}). "
                "Reduce the machine count or the sub-month split. Nothing was dispatched.",
                status_code=422, error_type="github_dispatch_payload_too_large")
        self.request("POST", self.repo_path(f"/actions/workflows/{self.config.workflow}/dispatches"),
                     body={"ref": self.config.ref, "inputs": dict(inputs)}, expect=(204, 200))

    def find_run(self, action: str, request_id: str, since: datetime) -> dict | None:
        """Newest workflow_dispatch run whose run-name is ``ob-<action>-<request_id>``."""
        created = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        wanted = run_name(action, request_id)
        matches: list[dict] = []
        for page in range(1, FIND_MAX_PAGES + 1):  # newest first; status snapshots add runs, so page a little
            query = urllib.parse.urlencode({"event": "workflow_dispatch", "created": f">={created}",
                                            "per_page": 100, "page": page})
            payload = self.request("GET", self.repo_path(f"/actions/workflows/{self.config.workflow}/runs?{query}")) or {}
            runs = payload.get("workflow_runs") or []
            matches += [run for run in runs if wanted in {str(run.get("display_title") or ""), str(run.get("name") or "")}]
            if matches or len(runs) < 100:
                break
        matches.sort(key=lambda run: str(run.get("created_at") or ""), reverse=True)
        return matches[0] if matches else None

    def get_run(self, run_id: int) -> dict:
        return self.request("GET", self.repo_path(f"/actions/runs/{int(run_id)}"))

    def result_artifact(self, run_id: int, request_id: str) -> dict:
        """Download + unzip ``ob-result-<request_id>`` and return its result.json."""
        name = f"ob-result-{request_id}"
        query = urllib.parse.urlencode({"name": name, "per_page": 10})
        listing = self.request("GET", self.repo_path(f"/actions/runs/{int(run_id)}/artifacts?{query}")) or {}
        artifacts = [item for item in listing.get("artifacts") or [] if item.get("name") == name and not item.get("expired")]
        if not artifacts:
            raise GitHubDispatchError(f"Run {run_id} has no artifact {name} (expired, or the job failed before uploading).",
                                      error_type="github_result_missing")
        artifact_id = int(artifacts[0]["id"])
        path = self.repo_path(f"/actions/artifacts/{artifact_id}/zip")
        status, headers, raw = self._transport("GET", self.config.api_url + path, self._headers(), None, self._timeout)
        if status in (301, 302, 303, 307, 308):
            location = next((value for key, value in headers.items() if key.lower() == "location"), "")
            if not _safe_location(location):
                raise GitHubDispatchError("artifact download redirect has no https location", error_type="github_result_missing")
            # The signed storage URL must NOT receive the GitHub token.
            status, _, raw = self._transport("GET", location, {"User-Agent": "mlops-dashboard-outerbounds-dispatch"}, None, self._timeout)
        if status != 200:
            raise self._error("GET", path, status, raw)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise GitHubDispatchError(f"artifact {name} is larger than {MAX_ARTIFACT_BYTES} bytes", error_type="github_result_invalid")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                member = next((item for item in archive.infolist() if item.filename.rsplit("/", 1)[-1] == "result.json"), None)
                if member is None or member.file_size > MAX_ARTIFACT_BYTES:
                    raise GitHubDispatchError(f"artifact {name} has no result.json", error_type="github_result_invalid")
                result = json.loads(archive.read(member))
        except (zipfile.BadZipFile, ValueError) as exc:
            raise GitHubDispatchError(f"artifact {name} is not a valid result: {type(exc).__name__}",
                                      error_type="github_result_invalid") from exc
        if not isinstance(result, dict):
            raise GitHubDispatchError(f"artifact {name} result.json is not an object", error_type="github_result_invalid")
        return result


# --- dispatch + wait ------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_inputs(action: str, request_id: str, *, flow: str = "", deployment_id: str = "",
                 params: Mapping[str, Any] | None = None, run_id: str = "") -> dict[str, str]:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("request_id must match ^[A-Za-z0-9_-]{8,64}$")
    return {
        "action": action,
        "flow": flow or "",
        "deployment_id": deployment_id or "",
        "params_json": json.dumps(dict(params), separators=(",", ":"), sort_keys=True) if params else "",
        "run_id": run_id or "",
        "request_id": request_id,
    }


def run_action(
    action: str,
    request_id: str,
    inputs: Mapping[str, str],
    *,
    client: GitHubClient | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Idempotently dispatch (or reuse) the run for ``request_id``, wait, and return its result.

    The returned dict is the workflow's result.json plus ``github_run_url``. Raises
    GitHubDispatchError (and subclasses) on GitHub/timeout problems; an action that ran
    but failed is returned with ``ok: False`` (the caller maps it).
    """
    client = client or GitHubClient(DispatchConfig.from_env(environ))
    log = log or (lambda message: None)
    total = float(timeout or _int_env("GITHUB_DISPATCH_TIMEOUT_SECONDS", 1200, 60, 3600, environ))
    find_window = float(_int_env("GITHUB_DISPATCH_FIND_SECONDS", 120, 15, 600, environ))
    poll = float(_int_env("GITHUB_DISPATCH_POLL_SECONDS", 5, 1, 60, environ))
    lookback = _int_env("GITHUB_DISPATCH_LOOKBACK_HOURS", 72, 1, 24 * 30, environ)
    started = clock()

    run = client.find_run(action, request_id, _now() - timedelta(hours=lookback))
    if run is not None:
        log(f"reusing GitHub run {run.get('id')} for request_id={request_id} (no new dispatch)")
    else:
        dispatched_at = _now() - timedelta(minutes=2)  # tolerate clock skew vs GitHub
        client.dispatch(inputs)
        log(f"dispatched {client.config.workflow}@{client.config.ref} action={action} request_id={request_id}")
        while run is None:
            if clock() - started > find_window:
                raise GitHubDispatchTimeout(
                    f"Dispatched {run_name(action, request_id)} but no matching run appeared within {find_window:g}s. "
                    f"Check {client.config.workflow_url}; retrying the same action reuses the run once it shows up.")
            sleep(min(poll, 5.0))
            run = client.find_run(action, request_id, dispatched_at)
    run_url = str(run.get("html_url") or client.config.workflow_url)
    log(f"waiting for GitHub run {run.get('id')} ({run_url})")
    while str(run.get("status")) != "completed":
        if clock() - started > total:
            raise GitHubDispatchTimeout(
                f"GitHub run {run.get('id')} for {run_name(action, request_id)} did not finish within {total:g}s "
                f"(status {run.get('status')}). It may still complete: {run_url}", run_url=run_url)
        sleep(poll)
        run = client.get_run(int(run["id"]))
    try:
        result = client.result_artifact(int(run["id"]), request_id)
    except GitHubDispatchError as exc:
        exc.run_url = run_url
        raise GitHubDispatchError(f"{exc} Run conclusion: {run.get('conclusion')}. {run_url}",
                                  status_code=exc.status_code, error_type=exc.error_type, run_url=run_url) from exc
    if result.get("request_id") not in (None, request_id):
        raise GitHubDispatchError(f"artifact request_id mismatch in run {run.get('id')}", error_type="github_result_invalid",
                                  run_url=run_url)
    return {**result, "github_run_id": run.get("id"), "github_run_url": run_url,
            "github_conclusion": run.get("conclusion")}


# --- child process (admin actions) ---------------------------------------------------------

def child_main(request: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict:
    values = _values(environ)
    op = str(request.get("op") or "")
    inputs_spec = request.get("inputs") or {}
    salt = str(values.get(ACTION_ID_ENV) or request.get("salt") or "")
    try:
        if op not in ACTIONS:
            raise ValueError(f"unknown op {op!r}")
        request_id = str(request.get("request_id") or "") or request_id_for(op, inputs_spec, salt)
        inputs = build_inputs(op, request_id, **inputs_spec)
        result = run_action(op, request_id, inputs, environ=environ,
                            log=lambda message: print(f"[gha-dispatch] {message}", flush=True))
        result.setdefault("request_id", request_id)
        if result.get("ok") and op == "trigger" and result.get("workflow_id"):
            print(f"Workflow triggered via GitHub Actions ({result.get('github_run_url')}).")
            print(f"Name: {result['workflow_id']}", flush=True)
        if result.get("ok") and op == "terminate":
            print(f"Terminated: {result.get('workflow_id')} via GitHub Actions ({result.get('github_run_url')})", flush=True)
        return result
    except GitHubDispatchError as exc:
        return {"ok": False, "op": op, "error_type": exc.error_type, "error": str(exc), "status_code": exc.status_code,
                "github_run_url": exc.run_url}
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        return {"ok": False, "op": op, "error_type": type(exc).__name__, "error": str(exc)[:2000]}


def main(argv: list[str]) -> int:
    if not argv:
        print(f"usage: python -m {CHILD_MODULE} '<json request>'", file=sys.stderr)
        return 2
    result = child_main(json.loads(argv[0]))
    token = os.environ.get(TOKEN_ENV, "")
    print(ACTION_MARKER + redact(json.dumps(result, default=str), (token,)), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
