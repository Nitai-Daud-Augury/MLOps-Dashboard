"""Databricks Apps workflow backend: Outerbounds actions through GitHub Actions.

Selected when ``runtime_mode`` reports ``workflow_backend == "github_actions"``
(``OUTERBOUNDS_DISPATCH_BACKEND=github_actions``, the Databricks default without a
Metaflow service key). The app holds no Outerbounds credential: each action
dispatches ``.github/workflows/outerbounds-dispatch.yml``, which logs in as the
OIDC machine user ``mlops-dashboard-sp`` and runs ``scripts/ob_dispatch.py``.

* trigger   - same ``argo submit -p`` parameters as the local builder (via
              ``outerbounds_actions.argo_submit_parameters``), dispatched as
              ``action=trigger``. The workflow checks the deployment's flow, its
              declared parameters and (orchestrator) the contract version.
* terminate - ``action=terminate`` with the configured deployment ids.
* status    - ``action=list_runs`` snapshots, cached (OUTERBOUNDS_GHA_STATUS_TTL_SECONDS,
              default 300) and refreshed in the background: a GitHub run takes
              minutes, so status requests never block on it.
* logs/create - not supported (open Outerbounds).

Triggers and terminates run as admin-action children
(``python -m backfill_dashboard.github_dispatch '<json>'``) through
``AdminActionRepository.run``, so the cancel window, duplicate-submit guard,
month reservations, sequential same-machine chunks and reconciliation apply
unchanged. ``AdminActionRepository.run`` passes the pending action id to the child
(``DASHBOARD_ADMIN_ACTION_ID``); the child derives ``request_id`` from it plus the
payload (machines, windows, deployment), finds an existing GitHub run with that
request_id before dispatching, and reuses it - a retried child never dispatches twice.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import github_dispatch as gha
from . import outerbounds_probe as probe
from .admin import (
    FullRlblTestParams,
    LogLookupParams,
    OUTERBOUNDS_RUNS_URL,
    RunningWorkflowProvider,
    TerminateParams,
    TriggerParams,
    ULRPM_ORCHESTRATOR_CONTRACT_VERSION,
    WorkflowCommandBuilder,
    WorkflowSourceRequest,
    parse_running_workflows,
    validate_workflow_id,
)
from .outerbounds_actions import (
    DEPLOYMENT_ENV,
    ChildInterpreterUnavailable,
    DeploymentNotConfigured,
    MetaflowCampaignAdapter,
    OperationNotSupported,
    WorkflowBackendError,
    argo_submit_parameters,
    configured_deployments,
    deployment_id,
)
from .runtime_mode import GITHUB_DISPATCH_NOT_CONFIGURED, github_dispatch_configured, github_dispatch_missing_message

BACKEND = "github_actions"
BACKEND_DIR = Path(__file__).resolve().parents[1]
KIND_FLOWS = {
    "fst_backfill": "FSTBackfill",
    "ulrpm_orchestrator": "UlrpmDevBackfillOrchestratorFlow",
    "fullrlbl_test": "FullRlblDevFstTestFlow",
}


class GitHubDispatchNotConfiguredError(WorkflowBackendError):
    status_code = 503
    error_type = GITHUB_DISPATCH_NOT_CONFIGURED


def _values(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    import os

    return os.environ if environ is None else environ


def require_dispatch(environ: Mapping[str, str] | None = None) -> gha.DispatchConfig:
    if not github_dispatch_configured(environ):
        raise GitHubDispatchNotConfiguredError(github_dispatch_missing_message(environ))
    try:
        return gha.DispatchConfig.from_env(environ)
    except gha.GitHubDispatchError as exc:
        raise GitHubDispatchNotConfiguredError(str(exc)) from exc


def _child_command(op: str, inputs: Mapping[str, Any]) -> tuple[Path, list[str]]:
    interpreter = probe.resolve_interpreter()
    if not interpreter.ok:
        raise ChildInterpreterUnavailable(f"dispatch child interpreter unavailable: {interpreter.error}")
    payload = json.dumps({"op": op, "inputs": dict(inputs)}, separators=(",", ":"), sort_keys=True)
    size = len(payload)
    if size > gha.MAX_INPUTS_CHARS:
        raise WorkflowBackendError(
            f"Request too large for GitHub workflow_dispatch ({size} chars, max {gha.MAX_INPUTS_CHARS}). "
            "Reduce the machine count or the sub-month split. Nothing was dispatched.",
            status_code=422, error_type="github_dispatch_payload_too_large")
    return BACKEND_DIR, [interpreter.path, "-m", gha.CHILD_MODULE, payload]


class GithubActionsCommandBuilder(WorkflowCommandBuilder):
    """``WorkflowCommandBuilder`` for Databricks via GitHub Actions: same validation, dispatch children."""

    backend = BACKEND

    def __init__(self, *, environ: Mapping[str, str] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._environ = environ

    def _problem(self, exc: WorkflowBackendError) -> dict:
        return {"ready": False, "detail": str(exc), "error_type": exc.error_type, "backend": self.backend}

    # readiness (configuration only: a GitHub run takes minutes, so nothing is dispatched here)
    def platform_readiness(self) -> dict:
        try:
            config = require_dispatch(self._environ)
        except WorkflowBackendError as exc:
            return self._problem(exc)
        deployments = configured_deployments(self._environ)
        return {
            "ready": True,
            "backend": self.backend,
            "detail": (f"Outerbounds actions dispatch {config.workflow}@{config.ref} in {config.repo} (GitHub Actions, "
                       f"OIDC machine user). Deployments set: {', '.join(sorted(deployments)) or 'none'}. "
                       "Verify with Admin > Outerbounds probe."),
            "deployments": deployments,
            "workflow_url": config.workflow_url,
        }

    def _deployment_readiness(self, kind: str) -> tuple[str | None, dict | None]:
        readiness = self.platform_readiness()
        if not readiness["ready"]:
            return None, readiness
        try:
            return deployment_id(kind, self._environ), None
        except WorkflowBackendError as exc:
            return None, self._problem(exc)

    def ulrpm_orchestrator_readiness(self) -> dict:
        deployment, problem = self._deployment_readiness("ulrpm_orchestrator")
        if problem:
            return problem
        return {
            "ready": True,
            "backend": self.backend,
            "workflow_template": deployment,
            # Not read up front (that would be a GitHub run per readiness poll): the dispatch
            # workflow checks the deployed contract right before triggering and refuses a
            # stale template (and sub-month splits on the legacy v4 contract).
            "contract_version": ULRPM_ORCHESTRATOR_CONTRACT_VERSION,
            "contract_check": "at_trigger",
            "supports_submonth_split": True,
            "detail": ("ULRPM parent orchestrator deployment configured; the dispatch workflow verifies its contract "
                       f"({ULRPM_ORCHESTRATOR_CONTRACT_VERSION}) before triggering."),
        }

    def fullrlbl_test_readiness(self) -> dict:
        deployment, problem = self._deployment_readiness("fullrlbl_test")
        if problem:
            return problem
        return {"ready": True, "backend": self.backend, "template_name": deployment,
                "detail": "FullRlbl test deployment configured; checked by the dispatch workflow at trigger time."}

    # actions
    def validate_cli(self, source: WorkflowSourceRequest) -> dict:
        return {"ready": True, "backend": self.backend,
                "detail": "No flow source is needed on Databricks: actions trigger the deployed template via GitHub Actions."}

    def build_create(self, source: WorkflowSourceRequest) -> tuple[Path, list[str]]:
        raise OperationNotSupported(
            "Create Workflow (argo-workflows create) compiles the flow source and is not available on Databricks. "
            "Deploy from a workstation, then set the matching OUTERBOUNDS_*_DEPLOYMENT_ID on the app."
        )

    def _trigger_command(self, kind: str, argo_command: list[str]) -> tuple[Path, list[str]]:
        require_dispatch(self._environ)
        deployment = deployment_id(kind, self._environ)
        return _child_command("trigger", {
            "flow": KIND_FLOWS[kind],
            "deployment_id": deployment,
            "params": argo_submit_parameters(argo_command),
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
        super().build_terminate(params)  # identical validation (prod confirmation, id format); builds nothing here
        workflow_id = params.workflow_id.strip()
        if not workflow_id:
            raise OperationNotSupported(
                "Stopping every run in a namespace uses the local bx_scripts Argo helper and is local-only. "
                "On Databricks stop runs individually (pass workflow_id)."
            )
        require_dispatch(self._environ)
        deployments = configured_deployments(self._environ)
        if not deployments:
            raise DeploymentNotConfigured("No OUTERBOUNDS_*_DEPLOYMENT_ID is set; terminate needs the deployment the run belongs to.")
        name = validate_workflow_id(workflow_id.removeprefix("argo-"))
        return _child_command("terminate", {"run_id": f"argo-{name}", "deployment_id": ",".join(sorted(set(deployments.values())))})

    def build_logs(self, source: WorkflowSourceRequest, params: LogLookupParams) -> tuple[Path, list[str]]:
        raise OperationNotSupported(
            "Task logs are not available through GitHub Actions dispatch (they would land in public workflow logs). "
            "Open the run in the Outerbounds UI."
        )


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


_TERMINAL = {"succeeded", "completed", "failed", "error", "terminated"}


class GithubActionsRunningWorkflowProvider(RunningWorkflowProvider):
    """Workflow status via cached GitHub Actions ``list_runs`` snapshots (never blocks on a run)."""

    def __init__(self, *, environ: Mapping[str, str] | None = None,
                 runner: Callable[..., dict] | None = None, start_thread: Callable[[Callable[[], None]], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._environ = environ
        self._runner = runner or gha.run_action
        self._start_thread = start_thread or (lambda target: threading.Thread(target=target, name="gha-status", daemon=True).start())
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self._fetched_at: float | None = None
        self._refreshing = False

    @property
    def ttl(self) -> int:
        return gha._int_env("OUTERBOUNDS_GHA_STATUS_TTL_SECONDS", 300, 60, 3600, self._environ)

    def _check_argo(self) -> bool:
        return github_dispatch_configured(self._environ)

    def _unavailable(self) -> dict:
        return {"workflows": [], "counts": {}, "argo_available": False, "workflow_backend": BACKEND,
                "error_type": GITHUB_DISPATCH_NOT_CONFIGURED, "hint": github_dispatch_missing_message(self._environ)}

    def _refresh(self) -> None:
        try:
            ttl = self.ttl
            limit = gha._int_env("OUTERBOUNDS_STATUS_RUN_LIMIT", 20, 1, 100, self._environ)
            # Same bucket -> same request_id: concurrent refreshes (or app replicas) share one GitHub run.
            bucket = int(time.time() // ttl)
            request_id = gha.request_id_for("list_runs", {"limit": limit, "bucket": bucket})
            inputs = gha.build_inputs("list_runs", request_id, params={"limit": limit})
            result = self._runner("list_runs", request_id, inputs, environ=self._environ,
                                  timeout=gha._int_env("OUTERBOUNDS_GHA_STATUS_TIMEOUT_SECONDS", 600, 60, 1800, self._environ))
            if result.get("ok"):
                snapshot = {"workflows": [asdict(item) for item in parse_running_workflows(result.get("workflows") or [])],
                            "warnings": list(result.get("warnings") or []), "github_run_url": result.get("github_run_url"),
                            "fetched_at": datetime.now(timezone.utc).isoformat()}
            else:
                snapshot = {"error": f"{result.get('error_type')}: {result.get('error')}", "error_type": result.get("error_type"),
                            "github_run_url": result.get("github_run_url"), "fetched_at": datetime.now(timezone.utc).isoformat()}
        except gha.GitHubDispatchError as exc:
            snapshot = {"error": str(exc), "error_type": exc.error_type, "github_run_url": exc.run_url,
                        "fetched_at": datetime.now(timezone.utc).isoformat()}
        except Exception as exc:  # noqa: BLE001 - a refresh must never crash the status route
            snapshot = {"error": f"{type(exc).__name__}: {gha.redact(exc)}", "error_type": "github_dispatch_failed",
                        "fetched_at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            previous = self._snapshot or {}
            if "error" in snapshot and previous.get("workflows") is not None:
                # Keep the last good list; surface the refresh error next to it.
                snapshot = {**previous, "refresh_error": snapshot["error"], "error_type": snapshot.get("error_type")}
            self._snapshot = snapshot
            self._fetched_at = self._clock()
            self._refreshing = False

    def _ensure_fresh(self) -> None:
        with self._lock:
            stale = self._fetched_at is None or self._clock() - self._fetched_at > self.ttl
            if not stale or self._refreshing:
                return
            self._refreshing = True
        self._start_thread(self._refresh)

    def _query(self, running_only: bool) -> dict:
        if not self._check_argo():
            return self._unavailable()
        self._ensure_fresh()
        with self._lock:
            snapshot = dict(self._snapshot or {})
            refreshing = self._refreshing
        payload: dict[str, Any] = {"argo_available": True, "workflow_backend": BACKEND, "outerbounds_url": OUTERBOUNDS_RUNS_URL,
                                   "refreshing": refreshing, "fetched_at": snapshot.get("fetched_at"),
                                   "github_run_url": snapshot.get("github_run_url")}
        warnings = list(snapshot.get("warnings") or [])
        warnings.append(f"Status comes from GitHub Actions snapshots (refreshed at most every {self.ttl}s); "
                        "it can lag the Outerbounds UI by a few minutes.")
        if snapshot.get("workflows") is None:
            payload.update(workflows=[], error=snapshot.get("error"), error_type=snapshot.get("error_type"))
            if not snapshot.get("error"):
                warnings.append("Fetching status through GitHub Actions (first snapshot takes ~2-4 minutes).")
        else:
            workflows = list(snapshot["workflows"])
            if running_only:
                workflows = [item for item in workflows if str(item.get("status") or "").lower() not in _TERMINAL]
            payload["workflows"] = workflows
            if snapshot.get("refresh_error"):
                payload.update(error=snapshot["refresh_error"], error_type=snapshot.get("error_type"))
            if not running_only:
                payload["counts"] = _counts(workflows)
        payload["warnings"] = warnings
        return payload

    def _list_running(self) -> dict:
        return self._query(running_only=True)

    def _list_recent(self) -> dict:
        return self._query(running_only=False)

    def lookup_workflows(self, workflow_ids: list[str]) -> list[dict]:
        ids = {item.removeprefix("argo-") for item in workflow_ids if item}
        ids = {item for item in ids if re.fullmatch(r"[A-Za-z0-9_.-]{3,253}", item)}
        if not ids or not self._check_argo():
            return []
        self._ensure_fresh()
        with self._lock:
            workflows = list((self._snapshot or {}).get("workflows") or [])
        # Same contract as the CLI path: absent/unknown never changes stored state.
        return [item for item in workflows if str(item.get("workflow_id") or "").removeprefix("argo-") in ids]


class GithubActionsCampaignAdapter(MetaflowCampaignAdapter):
    """Campaign dispatch stays refused on the GitHub Actions backend (see REASON)."""

    REASON = (
        "Campaign dispatch is not supported through GitHub Actions: campaign idempotency relies on a fixed Argo "
        "workflow name plus a later find(); the dispatch request_id only de-duplicates the GitHub run, not an Argo "
        "submission that succeeded while its result was lost, and GitHub results expire. A retry could duplicate a "
        "machine window. Use the per-machine / selected-machine backfill actions (ULRPM orchestrator) instead."
    )


__all__ = [
    "BACKEND",
    "DEPLOYMENT_ENV",
    "GitHubDispatchNotConfiguredError",
    "GithubActionsCampaignAdapter",
    "GithubActionsCommandBuilder",
    "GithubActionsRunningWorkflowProvider",
    "require_dispatch",
]
