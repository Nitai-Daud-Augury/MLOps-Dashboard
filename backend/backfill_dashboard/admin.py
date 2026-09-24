from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .config import EXPECTED_ULTRASONIC_V2_COLUMNS


SourceType = Literal["local", "github"]
WorkflowAction = Literal["create", "trigger", "logs", "manifest", "terminate"]
EnvironmentTarget = Literal["dev", "prod"]

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_METAFLOW_DIR = WORKSPACE_ROOT / "Augury repos" / "metaflow-bx"
DEFAULT_FLOW_PATH = DEFAULT_METAFLOW_DIR / "FSTBackfill_prod_flow.py"
PROD_NAMESPACE = "feature-store-container"
PROD_CONFIRMATION = "RUN_PROD_BACKFILL"
STOP_PROD_CONFIRMATION = "STOP_PROD_BACKFILL"
DEFAULT_DEV_NAMESPACE = "ulrpm-fst-dev-20260830"
ARGO_NAMESPACE = os.getenv("BACKFILL_ARGO_NAMESPACE", "jobs-default")
DEFAULT_FST_BACKFILL_TEMPLATE = os.getenv(
    "BACKFILL_FST_WORKFLOW_TEMPLATE",
    "fstbackfill.test.devcoreme.fstbackfill-zvsui",
)
DEFAULT_ULRPM_ORCHESTRATOR_TEMPLATE = os.getenv(
    "BACKFILL_ULRPM_ORCHESTRATOR_TEMPLATE",
    "fstbkfill.test.devfstm.ulrpmdeatorflow-hwqbr",
)
ULRPM_ORCHESTRATOR_CONTRACT_VERSION = "2026-09-17-v4"
DEFAULT_FULLRLBL_TEST_TEMPLATE = os.getenv(
    "BACKFILL_FULLRLBL_TEST_TEMPLATE",
    "fullrsttest.test.testulr.fullrlestflow-zsx6n",
)
OUTERBOUNDS_BASE_URL = "https://ui.augury.obp.outerbounds.com/dashboard/runs/p/default"
OUTERBOUNDS_RUNS_URL = f"{OUTERBOUNDS_BASE_URL}?flow_id=FSTBackfill"
OUTERBOUNDS_FULLRLBL_TEST_URL = f"{OUTERBOUNDS_BASE_URL}?flow_id=FullRlblDevFstTestFlow"


@dataclass
class WorkflowSourceRequest:
    source_type: SourceType = "local"
    local_flow_path: str = str(DEFAULT_FLOW_PATH)
    github_repo_url: str = ""
    github_ref: str = "master"
    github_flow_path: str = "FSTBackfill_prod_flow.py"


@dataclass
class TriggerParams:
    environment: EnvironmentTarget = "dev"
    namespace: str = DEFAULT_DEV_NAMESPACE
    storage_account_manifest_path: str = ""
    include_features_to_backfill: bool = False
    features_to_backfill: list[str] = field(default_factory=lambda: EXPECTED_ULTRASONIC_V2_COLUMNS.copy())
    max_parallel_steps: int = 1
    force_sessions_from_bucket: bool = False
    confirm_production: bool = False
    confirmation_text: str = ""


@dataclass
class LogLookupParams:
    run_task_path: str = ""
    stream: Literal["stdout", "stderr"] = "stdout"


@dataclass
class TerminateParams:
    environment: EnvironmentTarget = "dev"
    namespace: str = DEFAULT_DEV_NAMESPACE
    workflow_id: str = ""
    confirm_production: bool = False
    confirmation_text: str = ""


@dataclass
class FullRlblTestParams:
    machine_ids: list[str] = field(default_factory=list)
    fst_namespace: str = DEFAULT_DEV_NAMESPACE
    lst_namespace: str = "severity-relabel-dev-ulrpm"
    pipeline_name: str = ""
    manifest_path: str = ""
    runtime_patch: bool = False
    persist_dev_lst: bool = False
    seed_dev_lst: bool = False
    feature_fetch_mode: str = "legacy"
    test_mode: str = "fetch_only"
    wide_range_since: str = "2024/08/30/00"
    wide_range_until: str = "2026/08/30/00"
    memory_mb: int = 8192


@dataclass
class RunningWorkflow:
    workflow_id: str
    flow_name: str
    namespace: str
    status: str
    branch: str | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    outerbounds_url: str
    machine_ids: list[str] = field(default_factory=list)


@dataclass
class AdminAction:
    id: str
    action: WorkflowAction
    status: Literal["queued", "running", "succeeded", "failed"]
    command: list[str]
    cwd: str
    created_at: str
    finished_at: str | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    outerbounds_url: str | None = None
    machine_ids: list[str] = field(default_factory=list)
    workflow_id: str | None = None
    flow_name: str | None = None
    reservation_cleanup_pending: bool = False
    phase: str | None = None


class AdminActionRepository:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._lock = threading.Lock()
        self._actions: dict[str, AdminAction] = self._load()

    def list(self) -> list[dict]:
        with self._lock:
            return [asdict(action) for action in reversed(list(self._actions.values()))]

    def for_machine(self, machine_id: str) -> list[dict]:
        with self._lock:
            return [asdict(action) for action in reversed(list(self._actions.values())) if machine_id in action.machine_ids]

    def machine_ids_for_workflow(self, workflow_id: str) -> list[str]:
        with self._lock:
            for action in reversed(list(self._actions.values())):
                if action.workflow_id == workflow_id:
                    return action.machine_ids
        return []

    def active_workflow_ids(self, machine_id: str | None = None) -> list[str]:
        """Return submitted workflows that still need an authoritative status check."""
        with self._lock:
            return [
                action.workflow_id
                for action in self._actions.values()
                if action.action == "trigger"
                and action.workflow_id
                and action.status in {"queued", "running"}
                and (machine_id is None or machine_id in action.machine_ids)
            ]

    def sync_workflow_statuses(self, workflows: list[dict]) -> None:
        """Reconcile submitted Argo runs with their current workflow phase."""
        workflow_details = {str(workflow.get("workflow_id", "")): workflow for workflow in workflows}
        changed = False
        with self._lock:
            for action in self._actions.values():
                if action.action != "trigger" or not action.workflow_id or action.workflow_id not in workflow_details:
                    continue
                workflow = workflow_details[action.workflow_id]
                phase = str(workflow.get("status", "")).lower()
                next_status = "failed" if phase in {"failed", "error", "terminated"} else "succeeded" if phase in {"succeeded", "completed"} else "running"
                workflow_url = workflow.get("outerbounds_url")
                if isinstance(workflow_url, str) and workflow_url:
                    action.outerbounds_url = workflow_url
                if action.status != next_status:
                    action.status = next_status
                    action.finished_at = _now() if next_status in {"failed", "succeeded"} else None
                    changed = True
            if changed:
                self._persist()

    def get(self, action_id: str) -> dict | None:
        with self._lock:
            action = self._actions.get(action_id)
            return asdict(action) if action else None

    def mark_failed(
        self,
        action_id: str,
        error: str,
        *,
        reservation_cleanup_pending: bool = False,
    ) -> dict:
        """Persist a terminal failure for an action that cannot be submitted."""
        with self._lock:
            action = self._actions[action_id]
            action.status = "failed"
            action.finished_at = _now()
            action.error = error
            if action.action == "trigger":
                action.phase = "failed"
            action.reservation_cleanup_pending = reservation_cleanup_pending
            self._persist()
            return asdict(action)

    def mark_reservation_cleanup_pending(self, action_id: str) -> dict:
        with self._lock:
            action = self._actions[action_id]
            action.reservation_cleanup_pending = True
            self._persist()
            return asdict(action)

    def pending_reservation_cleanups(self) -> list[dict]:
        with self._lock:
            return [
                asdict(action)
                for action in self._actions.values()
                if action.action == "trigger" and action.reservation_cleanup_pending
            ]

    def mark_reservation_cleanup_complete(self, action_id: str) -> dict:
        with self._lock:
            action = self._actions[action_id]
            action.reservation_cleanup_pending = False
            self._persist()
            return asdict(action)

    def fail_stale_queued_triggers(
        self,
        *,
        stale_after_seconds: int = 30 * 60,
        now: datetime | None = None,
    ) -> list[dict]:
        """Fail trigger actions abandoned during preparation before Argo.

        ``run`` persists ``running`` before invoking Argo. A trigger that is
        still ``queued`` after the grace period therefore never attempted a
        submission and is safe to release, including actions in intermediate
        preparation phases. This distinction avoids guessing about a ``running``
        action that may have reached Argo before a process crash.
        """
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=stale_after_seconds)
        stale: list[dict] = []
        changed = False
        with self._lock:
            for action in self._actions.values():
                if (
                    action.action != "trigger"
                    or action.status != "queued"
                    or action.workflow_id
                    or action.phase not in {
                        None,
                        "queued",
                        "validating",
                        "preparing_manifests",
                        "submitting",
                    }
                ):
                    continue
                try:
                    created_at = datetime.fromisoformat(action.created_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if created_at > cutoff:
                    continue
                action.status = "failed"
                action.finished_at = _now()
                action.error = "Submission expired before the Argo command runner started"
                action.reservation_cleanup_pending = True
                action.phase = "failed"
                stale.append(asdict(action))
                changed = True
            if changed:
                self._persist()
        return stale

    def create(
        self,
        *,
        action: WorkflowAction,
        command: list[str],
        cwd: Path,
        outerbounds_url: str | None = None,
        machine_ids: list[str] | None = None,
        flow_name: str | None = None,
        phase: str | None = None,
        exclusive_trigger_submission: bool = False,
    ) -> AdminAction:
        item = AdminAction(
            id=uuid4().hex,
            action=action,
            status="queued",
            command=command,
            cwd=str(cwd),
            created_at=_now(),
            outerbounds_url=outerbounds_url,
            machine_ids=machine_ids or [],
            flow_name=flow_name,
            phase=phase,
        )
        with self._lock:
            if exclusive_trigger_submission:
                pending = next((
                    existing for existing in self._actions.values()
                    if existing.action == "trigger"
                    and existing.phase in {"queued", "validating", "preparing_manifests", "submitting"}
                ), None)
                if pending:
                    raise ValueError(f"Backfill trigger {pending.id} is already {pending.phase}")
            self._actions[item.id] = item
            self._persist()
        return item

    def update_trigger(
        self,
        action_id: str,
        *,
        phase: str,
        command: list[str] | None = None,
        cwd: Path | None = None,
        machine_ids: list[str] | None = None,
    ) -> dict:
        with self._lock:
            action = self._actions[action_id]
            if action.action != "trigger":
                raise ValueError("Only trigger actions support preparation phases")
            action.phase = phase
            if command is not None:
                action.command = command
            if cwd is not None:
                action.cwd = str(cwd)
            if machine_ids is not None:
                action.machine_ids = machine_ids
            self._persist()
            return asdict(action)

    def set_trigger_phase(self, action_id: str, phase: str) -> dict:
        return self.update_trigger(action_id, phase=phase)

    def record_completed(
        self,
        *,
        action: WorkflowAction,
        command: list[str],
        cwd: Path,
        stdout: str = "",
        outerbounds_url: str | None = None,
        machine_ids: list[str] | None = None,
        flow_name: str | None = None,
    ) -> AdminAction:
        item = AdminAction(
            id=uuid4().hex,
            action=action,
            status="succeeded",
            command=command,
            cwd=str(cwd),
            created_at=_now(),
            finished_at=_now(),
            stdout=stdout,
            outerbounds_url=outerbounds_url,
            machine_ids=machine_ids or [],
            flow_name=flow_name,
        )
        with self._lock:
            self._actions[item.id] = item
            self._persist()
        return item

    def run(self, action_id: str) -> None:
        with self._lock:
            action = self._actions[action_id]
            action.status = "running"
            self._persist()

        try:
            result = subprocess.run(
                action.command,
                cwd=action.cwd,
                capture_output=True,
                text=True,
                timeout=60 * 60 * 6,
            )
            with self._lock:
                action.stdout = result.stdout or ""
                action.stderr = result.stderr or ""
                action.finished_at = _now()
                if result.returncode == 0:
                    action.workflow_id = _workflow_id_from_submit_output(action.stdout)
                    # `argo submit` returning only confirms submission; the workflow remains active.
                    if action.action == "trigger":
                        action.status = "running" if action.workflow_id else "failed"
                        action.phase = "accepted" if action.workflow_id else "failed"
                        if not action.workflow_id:
                            action.error = "Argo submission returned success without a workflow ID"
                    else:
                        action.status = "succeeded"
                else:
                    action.status = "failed"
                    action.error = f"Command exited with {result.returncode}"
                    if action.action == "trigger":
                        action.phase = "failed"
                self._persist()
        except Exception as exc:
            with self._lock:
                action.status = "failed"
                action.finished_at = _now()
                action.error = str(exc)
                if action.action == "trigger":
                    action.phase = "failed"
                self._persist()

    def _load(self) -> dict[str, AdminAction]:
        if not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            for item in raw:
                item.setdefault("machine_ids", [])
                item.setdefault("workflow_id", _workflow_id_from_submit_output(item.get("stdout", "")))
                item.setdefault("flow_name", None)
                item.setdefault("reservation_cleanup_pending", False)
                item.setdefault("phase", None)
                if item.get("outerbounds_url"):
                    item["outerbounds_url"] = _normalize_outerbounds_url(item["outerbounds_url"])
            return {item["id"]: AdminAction(**item) for item in raw}
        except Exception:
            return {}

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [asdict(action) for action in self._actions.values()]
        self.state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


class WorkflowReviewRepository:
    """Small durable acknowledgement store for workflows reviewed by an operator."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._lock = threading.Lock()
        self._reviews = self._load()

    def reviewed_ids(self) -> list[str]:
        with self._lock:
            return list(self._reviews)

    def mark_reviewed(self, workflow_id: str) -> list[str]:
        validate_workflow_id(workflow_id)
        with self._lock:
            if workflow_id in self._reviews:
                return list(self._reviews)
            self._reviews[workflow_id] = _now()
            self._persist()
            return list(self._reviews)

    def clear_reviewed(self, workflow_id: str) -> list[str]:
        validate_workflow_id(workflow_id)
        with self._lock:
            if workflow_id not in self._reviews:
                return list(self._reviews)
            self._reviews.pop(workflow_id, None)
            self._persist()
            return list(self._reviews)

    def _load(self) -> dict[str, str]:
        if not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._reviews, indent=2, sort_keys=True), encoding="utf-8")


class WorkflowCommandBuilder:
    def __init__(self, *, fst_backfill_template: str | None = None, ulrpm_orchestrator_template: str | None = None, fullrlbl_test_template: str | None = None) -> None:
        self.fst_backfill_template = fst_backfill_template or DEFAULT_FST_BACKFILL_TEMPLATE
        self.ulrpm_orchestrator_template = ulrpm_orchestrator_template or DEFAULT_ULRPM_ORCHESTRATOR_TEMPLATE
        self.fullrlbl_test_template = fullrlbl_test_template or DEFAULT_FULLRLBL_TEST_TEMPLATE

    def resolve_source(self, source: WorkflowSourceRequest) -> tuple[Path, Path]:
        if source.source_type == "local":
            flow_path = _safe_workspace_path(source.local_flow_path)
            if not flow_path.exists():
                raise FileNotFoundError(f"Flow file not found: {flow_path}")
            return flow_path.parent, flow_path

        repo_dir = _github_checkout(source.github_repo_url, source.github_ref)
        flow_path = (repo_dir / source.github_flow_path).resolve()
        if not str(flow_path).startswith(str(repo_dir.resolve())):
            raise ValueError("github_flow_path must stay inside the cloned repository")
        if not flow_path.exists():
            raise FileNotFoundError(f"Flow file not found in checkout: {source.github_flow_path}")
        return flow_path.parent, flow_path

    def build_create(self, source: WorkflowSourceRequest) -> tuple[Path, list[str]]:
        cwd, flow_path = self.resolve_source(source)
        return cwd, [_python_executable(), str(flow_path), "--no-pylint", "argo-workflows", "create"]

    def validate_cli(self, source: WorkflowSourceRequest) -> dict:
        cwd, flow_path = self.resolve_source(source)
        result = subprocess.run(
            [_python_executable(), str(flow_path), "--help"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            output = f"{result.stderr}\n{result.stdout}"
            if "VegaChart" in output:
                raise RuntimeError(
                    "Backfill is unavailable: the local Metaflow and Outerbounds extension versions "
                    "are incompatible (missing VegaChart). Update the shared metaflow-bx environment "
                    "before submitting a workflow."
                )
            raise RuntimeError(f"Backfill CLI preflight failed: {output.strip()[-800:]}")
        return {"ready": True, "flow_path": str(flow_path)}

    def platform_readiness(self) -> dict:
        command = ["argo", "list", "-n", ARGO_NAMESPACE, "--running", "-o", "json"]
        if shutil.which("argo") is None:
            return {"ready": False, "detail": "argo CLI is not installed for the dashboard service."}
        try:
            subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        except Exception as exc:
            return {"ready": False, "detail": f"Argo authorization check failed: {exc}"}
        template_command = [
            "kubectl",
            "get",
            "workflowtemplate",
            "-n",
            ARGO_NAMESPACE,
            self.fst_backfill_template,
            "-o",
            "name",
        ]
        try:
            subprocess.run(template_command, capture_output=True, text=True, check=True, timeout=30)
        except Exception as exc:
            return {
                "ready": False,
                "detail": f"FSTBackfill Outerbounds template is unavailable: {exc}",
            }
        return {
            "ready": True,
            "detail": "Argo authorization and the deployed FSTBackfill Outerbounds template are ready.",
            "workflow_template": self.fst_backfill_template,
        }

    def ulrpm_orchestrator_readiness(self) -> dict:
        readiness = self.platform_readiness()
        if not readiness["ready"]:
            return readiness
        command = [
            "kubectl",
            "get",
            "workflowtemplate",
            "-n",
            ARGO_NAMESPACE,
            self.ulrpm_orchestrator_template,
            "-o",
            "json",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
            template = json.loads(result.stdout or "{}")
        except Exception as exc:
            return {
                "ready": False,
                "detail": f"ULRPM parent orchestrator template is unavailable: {exc}",
            }
        parameters = {
            str(item.get("name")): item.get("value")
            for item in template.get("spec", {}).get("arguments", {}).get("parameters", [])
            if isinstance(item, dict)
        }
        deployed_contract = str(parameters.get("orchestrator_contract_version", "")).strip('"')
        if deployed_contract != ULRPM_ORCHESTRATOR_CONTRACT_VERSION:
            return {
                "ready": False,
                "detail": (
                    "ULRPM parent orchestrator template is stale: expected contract "
                    f"{ULRPM_ORCHESTRATOR_CONTRACT_VERSION}, found {deployed_contract or 'none'}. "
                    "Redeploy UlrpmDevBackfillOrchestratorFlow.py before submitting."
                ),
                "workflow_template": self.ulrpm_orchestrator_template,
                "expected_contract": ULRPM_ORCHESTRATOR_CONTRACT_VERSION,
                "deployed_contract": deployed_contract or None,
            }
        return {
            "ready": True,
            "detail": "ULRPM parent orchestrator and child FSTBackfill template are ready.",
            "workflow_template": self.ulrpm_orchestrator_template,
            "contract_version": deployed_contract,
        }

    def build_trigger(self, source: WorkflowSourceRequest, params: TriggerParams) -> tuple[Path, list[str]]:
        if not params.storage_account_manifest_path.strip():
            raise ValueError("storage_account_manifest_path is required")
        if params.max_parallel_steps < 1:
            raise ValueError("max_parallel_steps must be >= 1")

        namespace = PROD_NAMESPACE if params.environment == "prod" else params.namespace.strip()
        if not namespace:
            raise ValueError("namespace is required")
        if namespace == PROD_NAMESPACE and (
            not params.confirm_production or params.confirmation_text != PROD_CONFIRMATION
        ):
            raise ValueError(f"Production trigger requires confirmation text: {PROD_CONFIRMATION}")
        if params.environment == "dev" and namespace == PROD_NAMESPACE:
            raise ValueError("Dev triggers cannot target feature-store-container")

        # The template was created from the compatible Outerbounds environment.
        # Submit it directly instead of importing the local Metaflow extension stack.
        command = [
            "argo",
            "submit",
            "-n",
            ARGO_NAMESPACE,
            "--from",
            f"workflowtemplate/{self.fst_backfill_template}",
            "-p",
            f"name-space={json.dumps(namespace)}",
            "-p",
            f"storage_account_manifest_path={json.dumps(params.storage_account_manifest_path.strip())}",
            "-p",
            # This controls manifest partitioning only. Campaign concurrency
            # lives in the durable dispatcher, outside the child flow.
            f"manifest_bucket_count={params.max_parallel_steps}",
            "-p",
            f"force_sessions_from_bucket={str(params.force_sessions_from_bucket).lower()}",
        ]
        features = [feature.strip() for feature in params.features_to_backfill if feature.strip()]
        feature_value = ",".join(features) if params.include_features_to_backfill else ""
        command.extend(["-p", f"features_to_backfill={json.dumps(feature_value)}"])
        return DEFAULT_METAFLOW_DIR, command

    def build_ulrpm_orchestrator_trigger(
        self,
        *,
        machine_ids: list[str],
        manifest_template: str,
        start_index: int,
        end_index: int,
        params: TriggerParams,
        month_indices_by_machine: dict[str, list[int]] | None = None,
    ) -> tuple[Path, list[str]]:
        if params.environment not in {"dev", "prod"}:
            raise ValueError("environment must be dev or prod")
        namespace = PROD_NAMESPACE if params.environment == "prod" else params.namespace.strip()
        if not namespace:
            raise ValueError("namespace is required")
        if namespace == PROD_NAMESPACE and (
            not params.confirm_production or params.confirmation_text != PROD_CONFIRMATION
        ):
            raise ValueError(f"Production trigger requires confirmation text: {PROD_CONFIRMATION}")
        if params.environment == "dev" and namespace == PROD_NAMESPACE:
            raise ValueError("Dev triggers cannot target feature-store-container")
        if not machine_ids:
            raise ValueError("At least one machine is required")
        if params.max_parallel_steps < 1:
            raise ValueError("Machine lane concurrency must be >= 1")
        selected_machine_count = len(set(machine_ids))
        if params.max_parallel_steps > selected_machine_count:
            raise ValueError(
                "Machine lane concurrency cannot exceed the selected machine count "
                f"({selected_machine_count})"
            )
        if params.force_sessions_from_bucket:
            raise ValueError("force_sessions_from_bucket is not supported by the ULRPM parent orchestrator.")

        batch_id = f"dashboard_ulrpm_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        command = [
            "argo",
            "submit",
            "-n",
            ARGO_NAMESPACE,
            "--from",
            f"workflowtemplate/{self.ulrpm_orchestrator_template}",
            "-p",
            f"machine_ids={json.dumps(','.join(machine_ids))}",
            "-p",
            f"name_space={json.dumps(namespace)}",
            "-p",
            f"manifest_template={json.dumps(manifest_template)}",
            "-p",
            f"start_index={start_index}",
            "-p",
            f"end_index={end_index}",
            "-p",
            f"machine_month_indices={json.dumps(json.dumps(month_indices_by_machine, separators=(',', ':')) if month_indices_by_machine else '')}",
            "-p",
            f"global_machine_concurrency={params.max_parallel_steps}",
            "-p",
            "child_max_parallel_steps=1",
            "-p",
            f"batch_id={json.dumps(batch_id)}",
            "-p",
            f"orchestrator_contract_version={json.dumps(ULRPM_ORCHESTRATOR_CONTRACT_VERSION)}",
            "-p",
            "redeploy_fst_template=false",
        ]
        return DEFAULT_METAFLOW_DIR, command

    def build_logs(self, source: WorkflowSourceRequest, params: LogLookupParams) -> tuple[Path, list[str]]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", params.run_task_path):
            raise ValueError("run_task_path must look like <run>/<step>/<task>")

        run_id = params.run_task_path.split("/")[0]
        argo_workflow_name = _strip_argo_prefix(run_id)

        if shutil.which("argo") is not None:
            command = [
                "argo",
                "logs",
                "-n",
                ARGO_NAMESPACE,
                argo_workflow_name,
            ]
            if params.stream == "stderr":
                command.append("--no-color")
            return DEFAULT_METAFLOW_DIR, command

        cwd, flow_path = self.resolve_source(source)
        stream_flag = "--stderr" if params.stream == "stderr" else "--stdout"
        return cwd, [_python_executable(), str(flow_path), "logs", params.run_task_path, stream_flag]

    def fullrlbl_test_readiness(self) -> dict:
        readiness = self.platform_readiness()
        if not readiness["ready"]:
            return readiness
        command = [
            "kubectl", "get", "workflowtemplate",
            "-n", ARGO_NAMESPACE,
            self.fullrlbl_test_template,
            "-o", "name",
        ]
        try:
            subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        except Exception as exc:
            return {
                "ready": False,
                "detail": (
                    f"FullRlbl test template '{self.fullrlbl_test_template}' is unavailable: {exc}. "
                    "Deploy the flow first from the WS: python FullRlblDevFstTestFlow.py "
                    "--package-suffixes=.yaml,.py --no-pylint --branch test_ulrpm_dev_fst "
                    "--with retry argo-workflows create"
                ),
                "template_name": self.fullrlbl_test_template,
            }
        return {
            "ready": True,
            "detail": "FullRlbl test template is deployed and ready.",
            "template_name": self.fullrlbl_test_template,
        }

    def build_fullrlbl_test_trigger(self, params: FullRlblTestParams) -> tuple[Path, list[str]]:
        if not params.machine_ids:
            raise ValueError("At least one machine ID is required")
        if not params.fst_namespace or params.fst_namespace == PROD_NAMESPACE:
            raise ValueError("fst_namespace must be a dev namespace, not empty or production")
        if params.fst_namespace == "ulrpm-fst-dev-20260830-feature-store":
            raise ValueError("Pass the FST namespace, not the container name")
        pipeline_name = params.pipeline_name.strip()
        if not pipeline_name:
            pipeline_name = f"fullrlbl-dev-fst-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

        command = [
            "argo", "submit",
            "-n", ARGO_NAMESPACE,
            "--from", f"workflowtemplate/{self.fullrlbl_test_template}",
            "-p", f"machine-ids={json.dumps(','.join(params.machine_ids))}",
            "-p", f"fst-namespace={json.dumps(params.fst_namespace)}",
            "-p", f"lst-namespace={json.dumps(params.lst_namespace)}",
            "-p", f"pipeline-name={json.dumps(pipeline_name)}",
            "-p", f"runtime-patch={json.dumps(str(params.runtime_patch).lower())}",
            "-p", f"persist-dev-lst={json.dumps(str(params.persist_dev_lst).lower())}",
            "-p", f"seed-dev-lst={json.dumps(str(params.seed_dev_lst).lower())}",
            "-p", f"feature-fetch-mode={json.dumps(params.feature_fetch_mode)}",
            "-p", f"test-mode={json.dumps(params.test_mode)}",
            "-p", f"wide-range-since={json.dumps(params.wide_range_since)}",
            "-p", f"wide-range-until={json.dumps(params.wide_range_until)}",
        ]
        if params.manifest_path.strip():
            command.extend(["-p", f"manifest-path={json.dumps(params.manifest_path.strip())}"])

        return DEFAULT_METAFLOW_DIR, command

    def build_terminate(self, params: TerminateParams) -> tuple[Path, list[str]]:
        namespace = PROD_NAMESPACE if params.environment == "prod" else params.namespace.strip()
        workflow_id = params.workflow_id.strip()
        if not namespace and not workflow_id:
            raise ValueError("namespace is required")
        if namespace == PROD_NAMESPACE and (
            not params.confirm_production or params.confirmation_text != STOP_PROD_CONFIRMATION
        ):
            raise ValueError(f"Production stop requires confirmation text: {STOP_PROD_CONFIRMATION}")
        if params.environment == "dev" and namespace == PROD_NAMESPACE:
            raise ValueError("Dev stop cannot target feature-store-container")
        if workflow_id:
            validate_workflow_id(workflow_id)
            command = [
                "argo",
                "terminate",
                "-n",
                ARGO_NAMESPACE,
                "--field-selector",
                f"metadata.name={workflow_id}",
            ]
        else:
            command = [
                _python_executable(),
                "-c",
                (
                    "from bx_scripts.argo_handler import terminate_workflow; "
                    f"terminate_workflow({namespace!r})"
                ),
            ]
        return DEFAULT_METAFLOW_DIR, [
            *command,
        ]


class RunningWorkflowProvider:
    def __init__(self) -> None:
        self._argo_available: bool | None = None
        self._cache_lock = threading.Lock()
        self._running_cache: tuple[float, dict] | None = None
        self._recent_cache: tuple[float, dict] | None = None

    def _check_argo(self) -> bool:
        if self._argo_available is None:
            self._argo_available = shutil.which("argo") is not None
        return self._argo_available

    def list_running(self) -> dict:
        return self._cached("running", ttl_seconds=10, loader=self._list_running)

    def _list_running(self) -> dict:
        command = ["argo", "list", "-n", ARGO_NAMESPACE, "--running", "-o", "json"]
        if not self._check_argo():
            return {
                "workflows": [],
                "argo_available": False,
                "hint": f"argo CLI not found locally. View running workflows in Outerbounds: {OUTERBOUNDS_RUNS_URL}&status=running",
                "command": command,
            }
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except Exception as exc:
            return {
                "workflows": [],
                "argo_available": True,
                "error": str(exc),
                "command": command,
            }

        try:
            workflows = parse_running_workflows(json.loads(result.stdout or "[]"))
        except Exception as exc:
            return {
                "workflows": [],
                "argo_available": True,
                "error": f"Unable to parse argo output: {exc}",
                "command": command,
            }
        return {"workflows": [asdict(workflow) for workflow in workflows], "argo_available": True, "command": command}

    def list_recent(self) -> dict:
        return self._cached("recent", ttl_seconds=20, loader=self._list_recent)

    def lookup_workflows(self, workflow_ids: list[str]) -> list[dict]:
        """Read the live phase for specific dashboard-submitted workflows.

        ``argo list --completed`` is intentionally capped for dashboard
        responsiveness, so it can omit an older workflow that has just reached
        a terminal phase.  Machine-level Running views reconcile their locally
        active IDs with this exact lookup instead.
        """
        ids = list(dict.fromkeys(workflow_id for workflow_id in workflow_ids if workflow_id))
        if not ids or not self._check_argo():
            return []

        def query(workflow_id: str) -> list[RunningWorkflow]:
            command = ["argo", "get", workflow_id, "-n", ARGO_NAMESPACE, "--request-timeout", "20s", "-o", "json"]
            try:
                result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=25)
                return parse_running_workflows(json.loads(result.stdout or "[]"))
            except Exception:
                # Do not change a stored state when Argo is temporarily
                # unavailable or a retained workflow has been garbage-collected.
                return []

        with ThreadPoolExecutor(max_workers=min(8, len(ids))) as executor:
            workflows = [workflow for result in executor.map(query, ids) for workflow in result]
        return [asdict(workflow) for workflow in workflows]

    def _cached(self, cache_name: Literal["running", "recent"], *, ttl_seconds: int, loader) -> dict:
        """Coalesce dashboard polling so one Argo query serves concurrent browser requests."""
        with self._cache_lock:
            cached = self._running_cache if cache_name == "running" else self._recent_cache
            if cached and time.monotonic() < cached[0]:
                return copy.deepcopy(cached[1])
            payload = loader()
            cached_payload = copy.deepcopy(payload)
            if cache_name == "running":
                self._running_cache = (time.monotonic() + ttl_seconds, cached_payload)
            else:
                self._recent_cache = (time.monotonic() + ttl_seconds, cached_payload)
            return copy.deepcopy(cached_payload)

    def _list_recent(self) -> dict:
        """Read recent workflow phases from the Argo cluster that backs Outerbounds."""
        running_command = ["argo", "list", "-n", ARGO_NAMESPACE, "--running", "--request-timeout", "25s", "-o", "json"]
        completed_command = ["argo", "list", "-n", ARGO_NAMESPACE, "--completed", "--since", "365d", "--chunk-size", "10", "--request-timeout", "25s", "-o", "json"]
        if not self._check_argo():
            return {"workflows": [], "counts": {}, "argo_available": False, "hint": f"argo CLI not found locally. View workflow state in Outerbounds: {OUTERBOUNDS_RUNS_URL}", "command": running_command}

        def query(command: list[str]) -> list[RunningWorkflow]:
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=35)
            return parse_running_workflows(json.loads(result.stdout or "[]"))

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                running_future = executor.submit(query, running_command)
                completed_future = executor.submit(query, completed_command)
                workflows = [*running_future.result(), *completed_future.result()]
        except Exception as exc:
            return {"workflows": [], "counts": {}, "argo_available": True, "error": str(exc), "command": running_command}

        counts = {"active": 0, "completed": 0, "failed": 0}
        for workflow in workflows:
            phase = workflow.status.lower()
            if phase in {"succeeded", "completed"}:
                counts["completed"] += 1
            elif phase in {"failed", "error"}:
                counts["failed"] += 1
            else:
                counts["active"] += 1
        return {"workflows": [asdict(workflow) for workflow in workflows], "counts": counts, "argo_available": True, "command": running_command}


def admin_spec() -> dict:
    return {
        "default_local_flow_path": str(DEFAULT_FLOW_PATH),
        "default_dev_namespace": DEFAULT_DEV_NAMESPACE,
        "prod_namespace": PROD_NAMESPACE,
        "prod_confirmation": PROD_CONFIRMATION,
        "stop_prod_confirmation": STOP_PROD_CONFIRMATION,
        "default_features_to_backfill": EXPECTED_ULTRASONIC_V2_COLUMNS,
        "templates": {
            "fst_backfill": DEFAULT_FST_BACKFILL_TEMPLATE,
            "ulrpm_orchestrator": DEFAULT_ULRPM_ORCHESTRATOR_TEMPLATE,
            "fullrlbl_test": DEFAULT_FULLRLBL_TEST_TEMPLATE,
        },
        "fullrlbl_test": {
            "default_fst_namespace": DEFAULT_DEV_NAMESPACE,
            "default_lst_namespace": "severity-relabel-dev-ulrpm",
            "default_feature_fetch_mode": "legacy",
            "default_test_mode": "fetch_only",
            "default_memory_mb": 8192,
            "outerbounds_url": OUTERBOUNDS_FULLRLBL_TEST_URL,
            "template_name": DEFAULT_FULLRLBL_TEST_TEMPLATE,
        },
        "params": [
            {
                "name": "name-space",
                "description": "FST namespace/root. feature-store-container is production; any other value is dev/test.",
            },
            {
                "name": "storage_account_manifest_path",
                "description": "Path to manifest parquet in the fst-backfill Azure container.",
            },
            {
                "name": "features_to_backfill",
                "description": "Comma-separated feature whitelist. Empty means all features.",
            },
            {
                "name": "manifest_bucket_count",
                "description": "Manifest partition count only; campaign concurrency is controlled by the durable dispatcher.",
            },
            {
                "name": "force_sessions_from_bucket",
                "description": "Advanced fallback to list raw sessions from bucket when FST session IDs are missing.",
            },
        ],
        "known_no_data_note": "Some 2024 and early-2025 ULRPM months can legitimately have no samples/FE output; treat explicit no-data logs differently from failed backfill.",
        "outerbounds_url": OUTERBOUNDS_RUNS_URL,
        "outerbounds_running_url": f"{OUTERBOUNDS_RUNS_URL}&status=running",
        "outerbounds_past_url": f"{OUTERBOUNDS_RUNS_URL}&status=completed,failed,error",
        "outerbounds_base_url": OUTERBOUNDS_BASE_URL,
    }


def parse_running_workflows(payload: object) -> list[RunningWorkflow]:
    if isinstance(payload, dict):
        listed_items = payload.get("items")
        if isinstance(listed_items, list):
            items = listed_items
        elif isinstance(payload.get("metadata"), dict) and isinstance(payload.get("status"), dict):
            # `argo list -o json` returns {"items": [...]}, while
            # `argo get <workflow> -o json` returns one workflow object. Exact
            # lookups drive terminal-state reconciliation for durable month
            # reservations, so both response shapes must use the same parser.
            items = [payload]
        else:
            items = []
    else:
        items = payload
    if not isinstance(items, list):
        return []
    workflows: list[RunningWorkflow] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        labels = metadata.get("labels", {})
        annotations = metadata.get("annotations", {})
        name = str(metadata.get("name", ""))
        if not name:
            continue
        flow_name = annotations.get("metaflow/flow_name", "")
        branch = annotations.get("metaflow/branch_name") or annotations.get("git/branch") or annotations.get("branch")
        run_id = annotations.get("metaflow/run_id", f"argo-{name}")
        workflows.append(
            RunningWorkflow(
                workflow_id=name,
                flow_name=str(flow_name) or name,
                namespace=str(metadata.get("namespace") or ARGO_NAMESPACE),
                status=str(status.get("phase") or "Running"),
                branch=str(branch) if branch else None,
                created_at=metadata.get("creationTimestamp"),
                started_at=status.get("startedAt"),
                finished_at=status.get("finishedAt"),
                outerbounds_url=_outerbounds_run_url(flow_name, run_id),
                machine_ids=_machine_ids_from_workflow(item),
            )
        )
    return workflows


def _machine_ids_from_workflow(item: dict) -> list[str]:
    """Read machine IDs from Argo labels, annotations, and submitted parameters.

    The ULRPM parent uses ``machine_ids`` while each FSTBackfill child commonly uses
    ``machine_id``. Argo exposes both in the workflow manifest, not just in logs.
    """
    values: list[str] = []
    keys = {"machineid", "machineids", "machineidlist", "machineidslist"}

    def visit(value: object, key: str = "") -> None:
        normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
        if isinstance(value, dict):
            parameter_name = value.get("name")
            parameter_key = re.sub(r"[^a-z0-9]", "", str(parameter_name).lower()) if parameter_name else ""
            if parameter_key in keys and "value" in value:
                visit(value["value"], str(parameter_name))
        if normalized_key in keys:
            if isinstance(value, str):
                cleaned = value.strip()
                # Argo preserves CLI parameter JSON quoting (for example, `"<machine-id>"`).
                if len(cleaned) >= 2 and cleaned[0] == '"' and cleaned[-1] == '"':
                    try:
                        cleaned = json.loads(cleaned)
                    except json.JSONDecodeError:
                        cleaned = cleaned.strip('"')
                values.extend(part.strip().strip('"').strip("'") for part in cleaned.split(",") if part.strip())
            elif isinstance(value, list):
                values.extend(str(part).strip() for part in value if str(part).strip())
        if isinstance(value, str):
            # Each child FSTBackfill receives exactly one manifest whose path includes
            # `monthly_<short-id>_<full-machine-id>/...`. This remains available in
            # the Argo workflow spec even though the child flow has no machine_id arg.
            values.extend(re.findall(r"(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])", value.lower()))
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, list):
            for child_value in value:
                visit(child_value, key)

    visit(item)
    return list(dict.fromkeys(values))


def validate_workflow_id(workflow_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,253}", workflow_id):
        raise ValueError("workflow_id must be 3-253 URL-safe characters")
    return workflow_id


def _looks_like_fst_backfill(name: str, labels: dict) -> bool:
    if not isinstance(labels, dict):
        labels = {}
    haystack = " ".join([name, *[str(value) for value in labels.values()]]).lower()
    return (
        "fstbackfill" in haystack
        or "fst-backfill" in haystack
        or "fst_backfill" in haystack
        or "fullrlbldevfsttest" in haystack
        or "fullrlbl-dev-fst-test" in haystack
    )


def _outerbounds_run_url(flow_name: str, run_id: str) -> str:
    if flow_name and run_id:
        return f"{OUTERBOUNDS_BASE_URL}/{flow_name}/{run_id}/view/run"
    return OUTERBOUNDS_RUNS_URL


def _strip_argo_prefix(run_id: str) -> str:
    return run_id.removeprefix("argo-")


def _normalize_outerbounds_url(url: str) -> str:
    return url.replace("flow_id=fst_backfill", "flow_id=FSTBackfill")


def _workflow_id_from_submit_output(stdout: str) -> str | None:
    """Extract the Argo workflow name emitted by `argo submit` when available."""
    match = re.search(r"^Name:\s*([A-Za-z0-9_.-]+)\s*$", stdout, re.MULTILINE)
    return match.group(1) if match else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _python_executable() -> str:
    return os.getenv("BACKFILL_ADMIN_PYTHON", str(WORKSPACE_ROOT / "Augury repos" / "metaflow-bx" / ".venv" / "bin" / "python"))


def _safe_workspace_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (WORKSPACE_ROOT / path).resolve()
    else:
        path = path.resolve()
    if not str(path).startswith(str(WORKSPACE_ROOT.resolve())):
        raise ValueError("Local flow path must stay inside this workspace")
    return path


def _github_checkout(repo_url: str, ref: str) -> Path:
    if not repo_url.startswith(("https://github.com/", "git@github.com:")):
        raise ValueError("Only GitHub repository URLs are allowed")
    if not re.fullmatch(r"[A-Za-z0-9._/@-]+", ref):
        raise ValueError("Invalid GitHub ref")

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_url.rstrip("/").split("/")[-1].replace(".git", ""))
    checkout_root = Path(os.getenv("BACKFILL_ADMIN_GITHUB_ROOT", "/tmp/backfill-dashboard-github"))
    target = checkout_root / f"{safe_name}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', ref)}"
    if target.exists():
        shutil.rmtree(target)
    checkout_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref, repo_url, str(target)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60 * 10,
    )
    return target.resolve()
