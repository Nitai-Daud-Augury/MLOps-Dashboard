"""Databricks Apps workflow backend: the Metaflow client instead of argo/kubectl.

Locally the dashboard drives Argo with the ``argo``/``kubectl`` CLIs (see
``admin.WorkflowCommandBuilder`` / ``admin.RunningWorkflowProvider``); that path
is unchanged. Databricks Apps has neither CLI nor a kube context, so when
``runtime_mode`` reports ``workflow_backend == "metaflow"`` the factory wires
in the classes below instead:

* trigger   - ``DeployedFlow.from_deployment(<deployment id>).trigger(**params)``.
              The parameters are derived from the exact ``argo submit -p`` list
              the local builder produces (same validation, same values), so both
              backends submit identical workflow arguments.
* status    - ``Flow(<flow>).runs()`` for listing; exact per-workflow phase (and
              the submitted parameters, for machine ownership) from the Metaflow
              Argo client, falling back to the Metaflow ``Run`` state.
* terminate - ``DeployedFlow.get_triggered_run(<deployment id>, "argo-<id>").terminate()``
              (ob-metaflow 2.19.34.1 ships ``ArgoWorkflowsTriggeredRun.terminate``).
              Only single runs; the namespace-wide stop stays local-only.
* logs      - ``Task(<flow>/<run>/<step>/<task>).stdout|stderr``.

Isolation: every Metaflow call runs in a short-lived child interpreter
(``python -m backfill_dashboard.outerbounds_actions '<json>'``), exactly like the
Outerbounds probe: the Outerbounds extension resolves its remote config
(including the service key) into ``os.environ`` and caches global state at
import, the Deployer API itself shells out to ``sys.executable``, and a hung call
must be killable. The child reuses the probe's interpreter resolution
(absolute ``sys.executable`` or ``OUTERBOUNDS_PROBE_PYTHON``), drops unrelated
secrets from its environment, works in a temp directory and prints one
``__OB_ACTION_RESULT__{json}`` line (redacted; never secrets).

Trigger/terminate/logs children run through ``AdminActionRepository.run`` like
the CLI commands they replace, so the cancel window, duplicate-submit guard,
month reservations and reconciliation in ``app.py`` apply unchanged.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

from . import outerbounds_probe as probe
from .admin import (
    FullRlblTestParams,
    LogLookupParams,
    OUTERBOUNDS_RUNS_URL,
    RunningWorkflowProvider,
    TerminateParams,
    TriggerParams,
    WorkflowCommandBuilder,
    WorkflowSourceRequest,
    evaluate_orchestrator_contract,
    parse_running_workflows,
    validate_workflow_id,
)
from .runtime_mode import SP_NOT_CONFIGURED, outerbounds_sp_configured, outerbounds_sp_missing_message

ACTION_MARKER = "__OB_ACTION_RESULT__"
CHILD_MODULE = "backfill_dashboard.outerbounds_actions"
BACKEND_DIR = Path(__file__).resolve().parents[1]

# Deployment identifiers = the Argo WorkflowTemplate names Metaflow created with
# `argo-workflows create` (what DeployedFlow.from_deployment expects).
DEPLOYMENT_ENV = {
    "fst_backfill": "OUTERBOUNDS_FSTBACKFILL_DEPLOYMENT_ID",
    "ulrpm_orchestrator": "OUTERBOUNDS_ULRPM_ORCHESTRATOR_DEPLOYMENT_ID",
    "fullrlbl_test": "OUTERBOUNDS_FULLRLBL_TEST_DEPLOYMENT_ID",
}
DEFAULT_STATUS_FLOWS = ("FSTBackfill", "UlrpmDevBackfillOrchestratorFlow", "FullRlblDevFstTestFlow")
TERMINATE_TOKEN_ENV = "OUTERBOUNDS_TERMINATE_AUTHORIZE_TOKEN"
_PARAM_VALUE_LIMIT = 8192
_LOG_LIMIT = 200_000


# --- errors ---------------------------------------------------------------------------

class WorkflowBackendError(RuntimeError):
    """A Databricks workflow-backend failure with an HTTP status and a stable type."""

    status_code = 502
    error_type = "metaflow_error"

    def __init__(self, message: str, *, status_code: int | None = None, error_type: str | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if error_type is not None:
            self.error_type = error_type


class ServicePrincipalNotConfigured(WorkflowBackendError):
    status_code = 503
    error_type = SP_NOT_CONFIGURED


class DeploymentNotConfigured(WorkflowBackendError):
    status_code = 503
    error_type = "deployment_not_configured"


class ChildInterpreterUnavailable(WorkflowBackendError):
    status_code = 503
    error_type = "interpreter_missing"


class OperationNotSupported(WorkflowBackendError):
    status_code = 501
    error_type = "not_supported_on_databricks"


class CampaignDispatchNotSupported(ValueError):
    """ValueError on purpose: the campaign Dispatcher treats it as permanent (no retry, no find)."""


def http_status_for(error_type: str, message: str = "") -> int:
    """Map a Metaflow/Outerbounds exception (type name + message) to an HTTP status."""
    name = (error_type or "").rsplit(".", 1)[-1]
    text = f"{name} {message}"
    if name in {"TimeoutError", "TimeoutExpired"} or "timed out" in message.lower():
        return 504
    if name == "RunIdMismatch":
        return 409
    if name in {"MetaflowNotFound", "LookupError"} or re.search(r"No deployed flow found|doesn't exist on Argo|Could not find workflow|No execution found", text):
        return 404
    if name in {"TypeError", "ValueError"}:
        return 422
    if re.search(r"\b(401|403)\b|Unauthori[sz]ed|Forbidden", text):
        return 502  # Outerbounds rejected the service principal: an upstream failure, not the caller's
    return 502


def error_from_child(result: Mapping[str, Any]) -> WorkflowBackendError:
    error_type = str(result.get("error_type") or "MetaflowError")
    message = str(result.get("error") or "Metaflow call failed")
    status = http_status_for(error_type, message)
    hint = ""
    if re.search(r"\b(401|403)\b|Unauthori[sz]ed|Forbidden", message):
        hint = " Outerbounds rejected the service principal; check its role/permissions in the Outerbounds perimeter."
    return WorkflowBackendError(f"{error_type}: {message}{hint}", status_code=status, error_type=error_type)


# --- configuration --------------------------------------------------------------------

def _values(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def deployment_id(kind: str, environ: Mapping[str, str] | None = None) -> str:
    variable = DEPLOYMENT_ENV[kind]
    value = str(_values(environ).get(variable) or "").strip()
    if not value:
        raise DeploymentNotConfigured(
            f"{variable} is not set: set it to the Outerbounds deployment (Argo WorkflowTemplate name) "
            f"to trigger on Databricks. See docs/DEPLOYMENT_RUNBOOK.md, 'Outerbounds service principal'."
        )
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,253}", value):
        raise DeploymentNotConfigured(f"{variable} is not a valid deployment identifier")
    return value


def configured_deployments(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    found = {}
    for kind in DEPLOYMENT_ENV:
        try:
            found[kind] = deployment_id(kind, environ)
        except DeploymentNotConfigured:
            continue
    return found


def status_flows(environ: Mapping[str, str] | None = None) -> list[str]:
    raw = str(_values(environ).get("OUTERBOUNDS_STATUS_FLOWS") or "")
    names = [item.strip() for item in raw.split(",") if item.strip()] or list(DEFAULT_STATUS_FLOWS)
    return [name for name in names if re.fullmatch(r"[A-Za-z0-9_]{1,128}", name)]


def _int_env(name: str, default: int, low: int, high: int, environ: Mapping[str, str] | None = None) -> int:
    try:
        value = int(str(_values(environ).get(name) or default))
    except ValueError:
        value = default
    return max(low, min(high, value))


def require_service_principal(environ: Mapping[str, str] | None = None) -> None:
    if not outerbounds_sp_configured(environ):
        raise ServicePrincipalNotConfigured(outerbounds_sp_missing_message(environ))


def _interpreter() -> probe.Interpreter:
    interpreter = probe.resolve_interpreter()
    if not interpreter.ok:
        raise ChildInterpreterUnavailable(f"Metaflow child interpreter unavailable: {interpreter.error}")
    return interpreter


def _payload(op: str, payload: Mapping[str, Any]) -> str:
    body = {"op": op, **payload}
    home = os.environ.get("METAFLOW_HOME")
    if home:
        body["metaflow_home"] = probe._absolute(home)
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def build_action_command(op: str, payload: Mapping[str, Any]) -> tuple[Path, list[str]]:
    """(cwd, argv) for a Metaflow action child run by AdminActionRepository.run."""
    interpreter = _interpreter()
    # cwd=backend dir so `-m backfill_dashboard...` resolves; the child moves to a temp dir at once.
    return BACKEND_DIR, [interpreter.path, "-m", CHILD_MODULE, _payload(op, payload)]


def run_query(op: str, payload: Mapping[str, Any], timeout: float | None = None) -> dict[str, Any]:
    """Run a read-only Metaflow query in a child process; raise WorkflowBackendError on failure."""
    interpreter = _interpreter()
    timeout = float(timeout or _int_env("OUTERBOUNDS_ACTION_QUERY_TIMEOUT_SECONDS", 60, 5, 300))
    command = [interpreter.path, "-m", CHILD_MODULE, _payload(op, payload)]
    secrets = tuple(item for item in (os.environ.get("METAFLOW_SERVICE_AUTH_KEY", ""),) if item)
    try:
        with tempfile.TemporaryDirectory(prefix="ob-action-") as workdir:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout,
                env=probe._child_env(probe.ProbeConfig.from_env()), cwd=workdir,
            )
    except subprocess.TimeoutExpired as exc:
        raise WorkflowBackendError(f"Metaflow {op} timed out after {timeout:g}s (child killed)", status_code=504, error_type="TimeoutError") from exc
    except OSError as exc:
        raise ChildInterpreterUnavailable(f"could not start {interpreter.path}: {type(exc).__name__}: {exc}") from exc
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(ACTION_MARKER):
            try:
                result = json.loads(line[len(ACTION_MARKER):])
            except ValueError:
                break
            if not result.get("ok"):
                raise error_from_child(result)
            return result
    tail = " | ".join((completed.stderr or completed.stdout or "").strip().splitlines()[-3:])
    raise WorkflowBackendError(probe._clip(probe.redact(f"Metaflow {op} child exited {completed.returncode}: {tail}", secrets), 1000))


# --- argo submit -> Metaflow trigger --------------------------------------------------

def argo_submit_parameters(command: list[str]) -> dict[str, Any]:
    """Workflow parameters of an ``argo submit ... -p name=value`` command.

    ``argo submit`` values are the JSON encoding the Argo template receives
    (strings quoted, numbers/bools bare). Decoding them gives the Python values
    Metaflow's ``trigger`` re-encodes with ``json.dumps`` - the same arguments.
    """
    parameters: dict[str, Any] = {}
    for index, item in enumerate(command):
        if item != "-p" or index + 1 >= len(command):
            continue
        name, _, raw = command[index + 1].partition("=")
        try:
            parameters[name] = json.loads(raw)
        except ValueError:
            parameters[name] = raw
    return parameters


def trigger_kwargs(parameters: Mapping[str, Any], param_info: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Map Argo parameter names/values onto ``DeployedFlow.trigger`` keyword arguments.

    ``param_info`` is the deployment's ``metaflow/parameters`` annotation; when
    known, values are coerced to the declared type and unknown names rejected.
    """
    declared = {}
    for details in (param_info or {}).values():
        if isinstance(details, dict) and details.get("name"):
            declared[str(details["name"])] = str(details.get("type") or "")
    kwargs: dict[str, Any] = {}
    for name, value in parameters.items():
        if declared and name not in declared:
            raise ValueError(f"deployment has no parameter {name!r} (declares: {', '.join(sorted(declared))})")
        kind = declared.get(name, "")
        if kind == "JSON" and isinstance(value, str):
            value = json.loads(value) if value else value
        elif kind == "int" and isinstance(value, str):
            value = int(value)
        elif kind == "float" and isinstance(value, (str, int)) and not isinstance(value, bool):
            value = float(value)
        elif kind == "bool" and isinstance(value, str):
            value = value.strip().lower() == "true"
        elif kind == "str" and not isinstance(value, str):
            value = json.dumps(value) if isinstance(value, (dict, list, bool)) or value is None else str(value)
        kwargs[name.replace("-", "_").lower()] = value
    return kwargs


# --- builder / providers used by the API on Databricks ----------------------------------

class MetaflowCommandBuilder(WorkflowCommandBuilder):
    """``WorkflowCommandBuilder`` for Databricks: same validation, Metaflow child commands."""

    backend = "metaflow"

    def __init__(self, *, environ: Mapping[str, str] | None = None,
                 runner: Callable[..., dict[str, Any]] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._environ = environ
        self._runner = runner or run_query
        self._template_cache: dict[str, tuple[float, dict]] = {}
        self._cache_lock = threading.Lock()

    # readiness ------------------------------------------------------------------------
    def platform_readiness(self) -> dict:
        try:
            require_service_principal(self._environ)
            _interpreter()
        except WorkflowBackendError as exc:
            return {"ready": False, "detail": str(exc), "error_type": exc.error_type, "backend": self.backend}
        deployments = configured_deployments(self._environ)
        return {
            "ready": True,
            "backend": self.backend,
            "detail": (
                "Outerbounds service principal configured; workflow actions use the Metaflow client "
                f"(deployments set: {', '.join(sorted(deployments)) or 'none'}). Verify with Admin > Outerbounds probe."
            ),
            "deployments": deployments,
        }

    def _template(self, deployment: str) -> dict:
        with self._cache_lock:
            cached = self._template_cache.get(deployment)
            if cached and time.monotonic() < cached[0]:
                return cached[1]
        result = self._runner("template", {"deployment_id": deployment})
        with self._cache_lock:
            self._template_cache[deployment] = (time.monotonic() + 60, result)
        return result

    def _deployment_readiness(self, kind: str) -> tuple[str | None, dict | None]:
        readiness = self.platform_readiness()
        if not readiness["ready"]:
            return None, readiness
        try:
            return deployment_id(kind, self._environ), None
        except WorkflowBackendError as exc:
            return None, {"ready": False, "detail": str(exc), "error_type": exc.error_type, "backend": self.backend}

    def ulrpm_orchestrator_readiness(self) -> dict:
        deployment, problem = self._deployment_readiness("ulrpm_orchestrator")
        if problem:
            return problem
        try:
            template = self._template(deployment)
        except WorkflowBackendError as exc:
            return {"ready": False, "backend": self.backend, "error_type": exc.error_type,
                    "detail": f"ULRPM parent orchestrator deployment {deployment} is unavailable: {exc}"}
        return {**evaluate_orchestrator_contract(template.get("parameters") or {}, deployment), "backend": self.backend}

    def fullrlbl_test_readiness(self) -> dict:
        deployment, problem = self._deployment_readiness("fullrlbl_test")
        if problem:
            return problem
        try:
            self._template(deployment)
        except WorkflowBackendError as exc:
            return {"ready": False, "backend": self.backend, "error_type": exc.error_type, "template_name": deployment,
                    "detail": f"FullRlbl test deployment {deployment} is unavailable: {exc}"}
        return {"ready": True, "backend": self.backend, "template_name": deployment,
                "detail": "FullRlbl test deployment is reachable through the Metaflow client."}

    # actions --------------------------------------------------------------------------
    def validate_cli(self, source: WorkflowSourceRequest) -> dict:
        return {"ready": True, "backend": self.backend,
                "detail": "No flow source is needed on Databricks: actions trigger the deployed template via the Metaflow client."}

    def build_create(self, source: WorkflowSourceRequest) -> tuple[Path, list[str]]:
        raise OperationNotSupported(
            "Create Workflow (argo-workflows create) compiles the flow source in the metaflow-bx environment and "
            "is not available on Databricks. Deploy from a workstation, then set the matching "
            "OUTERBOUNDS_*_DEPLOYMENT_ID on the app."
        )

    def _trigger_command(self, kind: str, argo_command: list[str]) -> tuple[Path, list[str]]:
        require_service_principal(self._environ)
        deployment = deployment_id(kind, self._environ)
        return build_action_command("trigger", {
            "deployment_id": deployment,
            "kind": kind,
            "parameters": argo_submit_parameters(argo_command),
        })

    def build_trigger(self, source: WorkflowSourceRequest, params: TriggerParams) -> tuple[Path, list[str]]:
        _, argo_command = super().build_trigger(source, params)
        return self._trigger_command("fst_backfill", argo_command)

    def build_ulrpm_orchestrator_trigger(self, **kwargs: Any) -> tuple[Path, list[str]]:
        _, argo_command = super().build_ulrpm_orchestrator_trigger(**kwargs)
        return self._trigger_command("ulrpm_orchestrator", argo_command)

    def build_fullrlbl_test_trigger(self, params: FullRlblTestParams) -> tuple[Path, list[str]]:
        _, argo_command = super().build_fullrlbl_test_trigger(params)
        return self._trigger_command("fullrlbl_test", argo_command)

    def build_terminate(self, params: TerminateParams) -> tuple[Path, list[str]]:
        super().build_terminate(params)  # identical validation (prod confirmation, id format); builds nothing on Databricks
        workflow_id = params.workflow_id.strip()
        if not workflow_id:
            raise OperationNotSupported(
                "Stopping every run in a namespace uses the local bx_scripts Argo helper and is local-only. "
                "On Databricks stop runs individually (pass workflow_id)."
            )
        require_service_principal(self._environ)
        deployments = configured_deployments(self._environ)
        if not deployments:
            raise DeploymentNotConfigured(
                "No OUTERBOUNDS_*_DEPLOYMENT_ID is set; terminate needs the deployment the run belongs to."
            )
        return build_action_command("terminate", {
            "workflow_id": validate_workflow_id(workflow_id.removeprefix("argo-")),
            "deployment_ids": sorted(set(deployments.values())),
        })

    def build_logs(self, source: WorkflowSourceRequest, params: LogLookupParams) -> tuple[Path, list[str]]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", params.run_task_path):
            raise ValueError("run_task_path must look like <run>/<step>/<task>")
        require_service_principal(self._environ)
        return build_action_command("logs", {
            "run_task_path": params.run_task_path,
            "stream": "stderr" if params.stream == "stderr" else "stdout",
            "flows": status_flows(self._environ),
        })


def _counts(workflows: list[dict]) -> dict[str, int]:
    counts = {"active": 0, "completed": 0, "failed": 0}
    for workflow in workflows:
        phase = str(workflow.get("status") or "").lower()
        if phase in {"succeeded", "completed"}:
            counts["completed"] += 1
        elif phase in {"failed", "error"}:
            counts["failed"] += 1
        else:
            counts["active"] += 1
    return counts


class MetaflowRunningWorkflowProvider(RunningWorkflowProvider):
    """Workflow status on Databricks via the Metaflow client (same payload shape as the argo CLI path)."""

    def __init__(self, *, environ: Mapping[str, str] | None = None,
                 runner: Callable[..., dict[str, Any]] | None = None) -> None:
        super().__init__()
        self._environ = environ
        self._runner = runner or run_query

    def _check_argo(self) -> bool:
        return outerbounds_sp_configured(self._environ)

    def _unavailable(self) -> dict:
        return {"workflows": [], "counts": {}, "argo_available": False, "workflow_backend": "metaflow",
                "error_type": SP_NOT_CONFIGURED, "hint": outerbounds_sp_missing_message(self._environ)}

    def _query(self, running_only: bool) -> dict:
        if not self._check_argo():
            return self._unavailable()
        try:
            result = self._runner("list", {
                "flows": status_flows(self._environ),
                "limit": _int_env("OUTERBOUNDS_STATUS_RUN_LIMIT", 20, 1, 200, self._environ),
                "running_only": running_only,
            })
        except WorkflowBackendError as exc:
            return {"workflows": [], "counts": {}, "argo_available": True, "workflow_backend": "metaflow",
                    "error": str(exc), "error_type": exc.error_type}
        workflows = [asdict(item) for item in parse_running_workflows(result.get("workflows") or [])]
        if running_only:
            workflows = [item for item in workflows if str(item.get("status") or "").lower() not in {"succeeded", "completed", "failed", "error", "terminated"}]
        payload = {"workflows": workflows, "argo_available": True, "workflow_backend": "metaflow",
                   "outerbounds_url": OUTERBOUNDS_RUNS_URL}
        if not running_only:
            payload["counts"] = _counts(workflows)
        if result.get("warnings"):
            payload["warnings"] = result["warnings"]
        return payload

    def _list_running(self) -> dict:
        return self._query(running_only=True)

    def _list_recent(self) -> dict:
        return self._query(running_only=False)

    def lookup_workflows(self, workflow_ids: list[str]) -> list[dict]:
        ids = list(dict.fromkeys(item.removeprefix("argo-") for item in workflow_ids if item))
        ids = [item for item in ids if re.fullmatch(r"[A-Za-z0-9_.-]{3,253}", item)]
        if not ids or not self._check_argo():
            return []
        try:
            result = self._runner("lookup", {"workflow_ids": ids, "flows": status_flows(self._environ)})
        except WorkflowBackendError:
            # Same contract as the CLI path: an unavailable lookup never changes stored state.
            return []
        return [asdict(item) for item in parse_running_workflows(result.get("workflows") or [])]


class MetaflowCampaignAdapter:
    """Campaign dispatch adapter for Databricks.

    The campaign dispatcher's idempotency relies on submitting with a fixed Argo
    workflow name (``argo submit --name fstbf-<key>``) and recovering it with
    ``find``. ``DeployedFlow.trigger`` cannot set the name, so a retried
    submission could start a duplicate run for the same machine window. Until
    that is solved, submissions are refused permanently with a clear reason
    (never retried, never silently dropped).
    """

    REASON = (
        "Campaign dispatch is not supported on Databricks yet: the Metaflow trigger API cannot set the "
        "idempotent workflow name campaigns rely on, so a retry could duplicate a machine window. "
        "Use the per-machine / selected-machine backfill actions (ULRPM orchestrator) instead."
    )

    def submit(self, item: dict):
        raise CampaignDispatchNotSupported(self.REASON)

    def find(self, item: dict):
        return None

    def statuses(self) -> dict[str, str]:
        return {}

    def cancel(self, workflow_name: str) -> None:
        raise OperationNotSupported(self.REASON)


# --- child process ----------------------------------------------------------------------

def _prepare_child(request: Mapping[str, Any]) -> None:
    for key in list(os.environ):
        if probe._UNRELATED_SECRET_RE.search(key):
            os.environ.pop(key, None)
    os.environ.pop("__REMOTE_CONFIG_HAS_BEEN_RESOLVED__", None)
    if request.get("metaflow_home"):
        os.environ["METAFLOW_HOME"] = str(request["metaflow_home"])
    os.chdir(tempfile.mkdtemp(prefix="ob-action-"))
    probe._bootstrap_remote_config_from_env(float(request.get("timeout") or 30))


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _sanitize_workflow(workflow: Mapping[str, Any]) -> dict:
    """Keep what parse_running_workflows needs; drop tokens and oversized values."""
    metadata = workflow.get("metadata") or {}
    annotations = {
        key: value for key, value in (metadata.get("annotations") or {}).items()
        if "token" not in key.lower() and len(str(value)) <= _PARAM_VALUE_LIMIT
    }
    parameters = []
    for item in ((workflow.get("spec") or {}).get("arguments") or {}).get("parameters") or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value", ""))
        if len(value) > _PARAM_VALUE_LIMIT:
            value = f"<omitted {len(value)} chars>"
        parameters.append({"name": item.get("name"), "value": value})
    status = workflow.get("status") or {}
    return {
        "metadata": {
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace"),
            "labels": metadata.get("labels") or {},
            "annotations": annotations,
            "creationTimestamp": metadata.get("creationTimestamp"),
        },
        "status": {"phase": status.get("phase"), "startedAt": status.get("startedAt"), "finishedAt": status.get("finishedAt")},
        "spec": {"arguments": {"parameters": parameters}},
    }


def _from_run(run: Any, flow: str) -> dict:
    run_id = str(run.id)
    phase = "Running" if not run.finished else ("Succeeded" if run.successful else "Failed")
    return {
        "metadata": {
            "name": run_id.removeprefix("argo-"),
            "annotations": {"metaflow/flow_name": flow, "metaflow/run_id": run_id},
            "creationTimestamp": _iso(run.created_at),
        },
        "status": {"phase": phase, "startedAt": _iso(run.created_at),
                   "finishedAt": _iso(getattr(run, "finished_at", None)) if run.finished else None},
        "source": "metaflow_run",
    }


def _argo_client():
    from metaflow.metaflow_config import KUBERNETES_NAMESPACE
    from metaflow.plugins.argo.argo_client import ArgoClient

    return ArgoClient(namespace=KUBERNETES_NAMESPACE)


def _op_template(request: Mapping[str, Any]) -> dict:
    deployment = str(request["deployment_id"])
    template = _argo_client().get_workflow_template(deployment)
    if template is None:
        raise LookupError(f"No deployed flow found for: {deployment}")
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    parameters = {
        str(item.get("name")): item.get("value")
        for item in ((template.get("spec") or {}).get("arguments") or {}).get("parameters") or []
        if isinstance(item, dict)
    }
    return {"ok": True, "deployment_id": deployment, "flow_name": annotations.get("metaflow/flow_name"),
            "parameters": parameters}


def _op_trigger(request: Mapping[str, Any]) -> dict:
    from metaflow import DeployedFlow

    deployment = str(request["deployment_id"])
    template = _argo_client().get_workflow_template(deployment)
    if template is None:
        raise LookupError(f"No deployed flow found for: {deployment}")
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    param_info = json.loads(annotations.get("metaflow/parameters") or "{}")
    kwargs = trigger_kwargs(request.get("parameters") or {}, param_info)
    triggered = DeployedFlow.from_deployment(deployment).trigger(**kwargs)
    pathspec = str(getattr(triggered, "pathspec", "") or "")
    flow, _, run_id = pathspec.partition("/")
    name = run_id.removeprefix("argo-")
    if not name:
        raise RuntimeError(f"trigger of {deployment} returned no run id")
    print(f"Workflow {deployment} triggered via the Metaflow client (pathspec {pathspec}).")
    print(f"Name: {name}", flush=True)
    return {"ok": True, "deployment_id": deployment, "flow": flow, "run_id": run_id, "workflow_id": name,
            "parameters": sorted(kwargs)}


def _lookup_run(flows: list[str], name: str) -> dict | None:
    from metaflow import Run
    from metaflow.exception import MetaflowNotFound

    for flow in flows:
        try:
            return _from_run(Run(f"{flow}/argo-{name}", _namespace_check=False), flow)
        except MetaflowNotFound:
            continue
    return None


def _op_lookup(request: Mapping[str, Any]) -> dict:
    flows = [str(item) for item in request.get("flows") or []]
    workflows, warnings = [], []
    client = None
    for name in [str(item) for item in request.get("workflow_ids") or []]:
        try:
            client = client or _argo_client()
            workflow = client.get_workflow(name)
            if workflow is not None:
                workflows.append(_sanitize_workflow(workflow))
            continue  # None: gone from Argo (garbage-collected/deleted) - reported as absent, like `argo get`
        except Exception as exc:  # noqa: BLE001 - fall back to the Metaflow run state
            warnings.append(f"argo phase unavailable for {name}: {probe._exc_type(exc)}")
        found = _lookup_run(flows, name)
        if found:
            workflows.append(found)
    return {"ok": True, "workflows": workflows, "warnings": warnings[:5]}


def _op_list(request: Mapping[str, Any]) -> dict:
    from metaflow import Flow
    from metaflow.exception import MetaflowNotFound

    limit = int(request.get("limit") or 20)
    running_only = bool(request.get("running_only"))
    workflows, warnings = [], []
    client, argo_ok = None, True
    for flow in [str(item) for item in request.get("flows") or []]:
        try:
            runs = list(itertools.islice(Flow(flow).runs(), limit))
        except MetaflowNotFound:
            continue
        for run in runs:
            if running_only and run.finished:
                continue
            run_id = str(run.id)
            if not run.finished and run_id.startswith("argo-") and argo_ok:
                try:
                    client = client or _argo_client()
                    workflow = client.get_workflow(run_id[5:])
                    if workflow is not None:
                        workflows.append(_sanitize_workflow(workflow))
                        continue
                except Exception as exc:  # noqa: BLE001
                    argo_ok = False
                    warnings.append(f"argo phase unavailable ({probe._exc_type(exc)}); using Metaflow run state")
            workflows.append(_from_run(run, flow))
    return {"ok": True, "workflows": workflows, "warnings": warnings}


def _op_terminate(request: Mapping[str, Any]) -> dict:
    from metaflow import DeployedFlow
    from metaflow.plugins.argo.argo_workflows import ArgoWorkflows

    name = str(request["workflow_id"]).removeprefix("argo-")
    execution = ArgoWorkflows.get_execution(name)
    if execution is None:
        raise LookupError(f"Could not find workflow {name} on Argo Workflows")
    flow_name = execution[2]
    token = os.environ.get(TERMINATE_TOKEN_ENV, "").strip()
    checked = []
    for deployment in [str(item) for item in request.get("deployment_ids") or []]:
        deployed = DeployedFlow.from_deployment(deployment)
        checked.append(f"{deployment}={deployed.deployer.flow_name}")
        if deployed.deployer.flow_name != flow_name:
            continue
        triggered = DeployedFlow.get_triggered_run(deployment, f"argo-{name}")
        ok = triggered.terminate(**({"authorize": token} if token else {}))
        if not ok:
            raise RuntimeError(
                f"Metaflow argo-workflows terminate failed for {flow_name}/argo-{name}. If the deployment was "
                f"created by another user, Metaflow requires its production token: set {TERMINATE_TOKEN_ENV} "
                "(secret) on the app, or stop the run from the Outerbounds UI."
            )
        print(f"Terminated: {name} ({flow_name}, deployment {deployment})", flush=True)
        return {"ok": True, "workflow_id": name, "flow": flow_name, "deployment_id": deployment}
    raise LookupError(
        f"Run argo-{name} belongs to flow {flow_name}, which matches no configured deployment ({', '.join(checked) or 'none'})."
    )


def _op_logs(request: Mapping[str, Any]) -> dict:
    from metaflow import Task
    from metaflow.exception import MetaflowNotFound

    path = str(request["run_task_path"])
    stream = "stderr" if request.get("stream") == "stderr" else "stdout"
    for flow in [str(item) for item in request.get("flows") or []]:
        try:
            task = Task(f"{flow}/{path}", _namespace_check=False)
        except MetaflowNotFound:
            continue
        text = str(getattr(task, stream) or "")
        print(probe._clip(text, _LOG_LIMIT), flush=True)
        return {"ok": True, "pathspec": f"{flow}/{path}", "stream": stream, "chars": len(text)}
    raise LookupError(f"Task {path} not found in flows: {', '.join(request.get('flows') or [])}")


_OPS: dict[str, Callable[[Mapping[str, Any]], dict]] = {
    "template": _op_template,
    "trigger": _op_trigger,
    "lookup": _op_lookup,
    "list": _op_list,
    "terminate": _op_terminate,
    "logs": _op_logs,
}


def child_main(request: Mapping[str, Any]) -> dict[str, Any]:
    """Executed in the child process. Returns a JSON-able dict; never secrets."""
    op = str(request.get("op") or "")
    try:
        if op not in _OPS:
            raise ValueError(f"unknown op {op!r}")
        _prepare_child(request)
        from metaflow import namespace

        namespace(None)  # read any user's runs (the SP is not their author)
        return _OPS[op](request)
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        return {"ok": False, "op": op, "error_type": probe._exc_type(exc), "error": probe._clip(str(exc), 2000)}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m backfill_dashboard.outerbounds_actions '<json request>'", file=sys.stderr)
        return 2
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    result = child_main(json.loads(argv[0]))
    secrets = tuple(item for item in (os.environ.get("METAFLOW_SERVICE_AUTH_KEY", ""), os.environ.get(TERMINATE_TOKEN_ENV, "")) if item)
    print(ACTION_MARKER + probe.redact(json.dumps(result), secrets), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
