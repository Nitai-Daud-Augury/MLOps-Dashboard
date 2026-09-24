from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
import asyncio
import os
import time
from uuid import uuid4
from pydantic import BaseModel, Field

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from .admin import (
    DEFAULT_FLOW_PATH,
    DEFAULT_DEV_NAMESPACE,
    OUTERBOUNDS_RUNS_URL,
    OUTERBOUNDS_FULLRLBL_TEST_URL,
    FullRlblTestParams,
    LogLookupParams,
    PROD_CONFIRMATION,
    PROD_NAMESPACE,
    TerminateParams,
    TriggerParams,
    WorkflowSourceRequest,
    admin_spec,
)
from .manifests import (
    MachineManifestRequest,
    MultiMachineManifestRequest,
    manifest_command_preview,
    manifest_stdout,
    orchestrator_month_index,
    orchestrator_month_windows,
    validate_machine_ids,
    validate_timestamp,
)
from .activity import effective_activity_status, is_month_backfillable
from .months import month_for_index
from .storage import AzureBlobStore
from .feature_series import ULTRASONIC_FEATURES, read_feature_series
from .deps import (
    admin_repository, backfill_actions, blob_discovery, command_builder, control_store,
    manifest_writer, repository, running_workflows, scanner, settings, workflow_reviews, control_plane,
)
from .runtime_mode import runtime_info
from .control_store import ACTIVE_MONTH_STATES
from .inventory_models import MachineSearchQuery
from .scanner import BackfillScanner
from .outerbounds_costs import current_cost_report
from .middleware import register_polling_guard
from .static_host import LOCAL_VITE_ORIGINS, local_vite_development_enabled, register_static_spa_host
from .control_plane.inventory_routes import etag_response
from .control_plane.routes import build_router as build_control_plane_router
from .control_plane.selection import iter_selection
from .schemas import (
    AllMachinesBackfillRequest, CancelMonthRequest, CreateWorkflowRequest,
    FullRlblTestRequestModel, LogsRequest, MachineManifestRequestModel, MonthPlanRequest,
    MultiMachineManifestRequestModel, OrchestratedBackfillRequest, OrchestratedManifestRequest,
    ScanRequest, TerminateParamsModel,
    TerminateWorkflowRequest, TriggerParamsModel, TriggerWorkflowRequest,
    WorkflowSourceModel,
)


RUNTIME_INFO = runtime_info()
app = FastAPI(title="MLOps Dashboard")
if local_vite_development_enabled():
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(LOCAL_VITE_ORIGINS),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
app.add_middleware(GZipMiddleware, minimum_size=500)


register_polling_guard(app)
app.router.routes.extend(build_control_plane_router(control_plane).routes)


def _workflow_mutation_route(method: str, path: str) -> bool:
    """Identify routes that launch local workflows or alter workflow state."""
    if method != "POST":
        return False
    fixed = {
        "/api/admin/workflows/preflight",
        "/api/admin/workflows/create",
        "/api/admin/workflows/trigger",
        "/api/admin/workflows/terminate",
        "/api/admin/logs",
        "/api/admin/backfills/all",
        "/api/admin/backfills/orchestrated",
        "/api/admin/fullrlbl/trigger",
        "/api/v1/backfill-campaigns",
    }
    if path in fixed:
        return True
    return (
        path.startswith("/api/machines/") and path.endswith("/cancel")
        or path.startswith("/api/v1/backfill-campaigns/")
        and (path.endswith("/actions") or "/items/" in path and "/actions" in path)
    )


def _monitor_only_workflow_hint() -> str:
    return f"Workflow operations are disabled in {RUNTIME_INFO.mode} runtime; open Outerbounds for live workflow status."


@app.middleware("http")
async def enforce_runtime_capabilities(request: Request, call_next):
    if _workflow_mutation_route(request.method, request.url.path) and not RUNTIME_INFO.workflow_mutations:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {
                "detail": (
                    f"{_monitor_only_workflow_hint()} This dashboard is monitor-only."
                )
            },
            status_code=403,
        )
    return await call_next(request)


@app.on_event("startup")
def start_control_plane() -> None:
    control_plane.runtime.start()


@app.on_event("shutdown")
def stop_control_plane() -> None:
    control_plane.runtime.stop()


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "runtime": RUNTIME_INFO.as_dict(),
        "source_account": settings.fst_account,
        "source_container": settings.fst_container,
    }


@app.get("/api/outerbounds/cost-report")
def outerbounds_cost_report() -> dict:
    """Return the configured, read-only Outerbounds cost-report payload."""
    return current_cost_report()


@app.get("/api/v1/inventory/status")
def inventory_status(request: Request, response: Response):
    return etag_response(request, response, control_plane.synchronizer.status())


@app.get("/api/v1/machines")
def list_inventory(request: Request, response: Response, cursor: str | None = None, limit: int = 100, search: str = "", cohort: str | None = None, status: str | None = None, eligible: bool | None = None, site_id: str | None = None, organization_id: str | None = None, classification_issue: str | None = None, sort_by: str = "machine_id", sort_dir: str = "asc"):
    cohort = cohort or None
    status = status or None
    if cohort not in {None, "standard", "ulrpm", "unknown"}:
        raise HTTPException(status_code=400, detail="cohort must be standard, ulrpm, or unknown")
    try:
        page = control_plane.inventory.search(MachineSearchQuery(cursor, min(max(limit, 1), 200), search, cohort, status, eligible, site_id, organization_id, classification_issue, sort_by, sort_dir))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    payload = {"machines": [asdict(machine) for machine in page.machines], "next_cursor": page.next_cursor, "inventory_version": page.inventory_version, "total_estimate": page.total_estimate}
    return etag_response(request, response, payload)


@app.get("/api/v1/machine-facets")
def inventory_facets(request: Request, response: Response, cohort: str | None = None, status: str | None = None, eligible: bool | None = None):
    facets = control_plane.inventory.get_facets(MachineSearchQuery(limit=200, cohort=cohort, status=status, eligible=eligible))
    return etag_response(request, response, asdict(facets))


@app.get("/api/backfill/status")
def backfill_status() -> dict:
    return repository.latest()


@app.get("/api/blob-sources/accounts")
def list_blob_accounts() -> dict:
    return {"accounts": blob_discovery.list_accounts()}


@app.get("/api/blob-sources/containers")
def list_blob_containers(account: str) -> dict:
    try:
        return {"account": account, "containers": blob_discovery.list_containers(account)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/backfill/scan")
def start_scan(background_tasks: BackgroundTasks, request: ScanRequest | None = None, sync: bool = False) -> dict:
    scan_settings = replace(
        scanner.settings,
        fst_account=request.source_account if request and request.source_account else scanner.settings.fst_account,
        fst_container=request.source_container if request and request.source_container else scanner.settings.fst_container,
    )
    scan_instance = BackfillScanner(
        settings=scan_settings,
        inventory=scanner.inventory,
        blob_store=AzureBlobStore(scan_settings.fst_account, scan_settings.fst_container),
        silver_provider=scanner.silver_provider,
        lifecycle_inventory=scanner.lifecycle_inventory,
        lifecycle_status=scanner.lifecycle_status,
    )
    state, is_new = repository.start_scan(
        source_account=scan_settings.fst_account,
        source_container=scan_settings.fst_container,
    )
    if not is_new:
        return {"scan_state": asdict(repository.scan_state), "already_running": True}
    if sync:
        repository.run_scan(scan_instance, state.scan_id)
    else:
        background_tasks.add_task(repository.run_scan, scan_instance, state.scan_id)
    return {"scan_state": asdict(repository.scan_state)}


@app.post("/api/backfill/scan/reset")
def reset_scan() -> dict:
    """Force-clear a stuck scan so a fresh one can start."""
    state = repository.reset_scan()
    return {"scan_state": asdict(state)}


@app.get("/api/backfill/status/{machine_id}")
def machine_status(machine_id: str) -> dict:
    payload = repository.latest()
    machines = payload["snapshot"].get("machines", [])
    for machine in machines:
        if machine["machine_id"] == machine_id:
            return {"scan_state": payload["scan_state"], "machine": machine}
    raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")


@app.get("/api/machines/{machine_id}/feature-series")
def machine_feature_series(
    machine_id: str,
    year: int = Query(ge=2000, le=2100),
    month: int = Query(ge=1, le=12),
    feature: str = Query(default="ultrasonic_p2p"),
    max_points: int = Query(default=2500, ge=100, le=5000),
) -> dict:
    """Return read-only v1/v2 chart values from a scanned monthly partition."""
    if feature not in ULTRASONIC_FEATURES:
        raise HTTPException(status_code=400, detail=f"Unsupported ultrasonic feature: {feature}")
    snapshot = repository.latest()["snapshot"]
    machine = next(
        (item for item in snapshot.get("machines", []) if item.get("machine_id") == machine_id),
        None,
    )
    if machine is None:
        raise HTTPException(status_code=404, detail=f"Machine not found: {machine_id}")
    selected_month = next(
        (
            item
            for item in machine.get("months", [])
            if item.get("partition", {}).get("year") == year
            and item.get("partition", {}).get("month") == month
        ),
        None,
    )
    if selected_month is None:
        raise HTTPException(status_code=404, detail=f"Month not found for machine: {year}-{month:02d}")
    blob_path = selected_month.get("blob_path")
    if not blob_path or selected_month.get("row_count") in (None, 0):
        raise HTTPException(status_code=404, detail="This month has no readable Feature Store partition")

    store = AzureBlobStore(snapshot["source_account"], snapshot["source_container"])
    try:
        blob = store.read_blob(blob_path)
        series = read_feature_series(blob.content, feature=feature, max_points=max_points)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not read feature values: {exc}") from exc
    return {
        "machine_id": machine_id,
        "month": f"{year}-{month:02d}",
        "source_account": snapshot["source_account"],
        "source_container": snapshot["source_container"],
        "blob_url": blob.url,
        "available_features": list(ULTRASONIC_FEATURES),
        **series,
    }


@app.get("/api/backfill/export/manifest")
def export_manifest(status: str = "needs_backfill") -> Response:
    payload = repository.latest()
    rows = ["machine_id,since,until"]
    for machine in payload["snapshot"].get("machines", []):
        for month in machine.get("months", []):
            if month.get("status") == status and is_month_backfillable(month):
                since, until = _month_bounds(month)
                rows.append(f"{machine['machine_id']},{since},{until}")
    return Response(
        "\n".join(rows) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ulrpm_{status}_manifest.csv"'},
    )


@app.get("/api/admin/spec")
def get_admin_spec() -> dict:
    return {**admin_spec(), "runtime": RUNTIME_INFO.as_dict()}

@app.post("/api/admin/backfills/month-plan")
def backfill_month_plan(request: MonthPlanRequest) -> dict:
    machines = {item["machine_id"]: item for item in repository.latest()["snapshot"].get("machines", [])}
    machine_ids = request.machine_ids or list(machines)
    if request.mode not in {"gaps", "all"}:
        raise HTTPException(status_code=400, detail="mode must be gaps or all")
    plan = {}
    for machine_id in machine_ids:
        machine = machines.get(machine_id)
        if not machine:
            continue
        allowed = {"needs_backfill"} if request.mode == "gaps" else {"needs_backfill", "backfilled"}
        # Snapshot partition indexes used to be relative to the first month in
        # a scan.  Resolve from the calendar fields so a retained pre-fix
        # snapshot cannot submit a shifted month to the parent orchestrator.
        indices = [
            orchestrator_month_index(month["partition"]["year"], month["partition"]["month"])
            for month in machine.get("months", [])
            if month.get("status") in allowed and is_month_backfillable(month)
        ]
        if indices:
            plan[machine_id] = indices
    return {"month_indices_by_machine": plan}


@app.get("/api/machines/{machine_id}/backfills/active")
def active_machine_backfills(machine_id: str) -> dict:
    """Return live month ownership; terminal parents never remain active here."""
    _release_stale_submission_reservations()
    try:
        records = control_store.list_machine_states(machine_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Backfill control store is unavailable: {exc}") from exc
    months = []
    terminal_phases = {"succeeded", "completed", "failed", "error", "terminated"}
    for record in records:
        # Control records are durable history. Only explicit active states
        # belong in this endpoint; otherwise a completed/terminated run can be
        # resurrected in Monitor when Argo is briefly unavailable.
        if record.get("state") not in ACTIVE_MONTH_STATES:
            continue
        parent_id = str(record.get("parent_workflow_id") or "")
        action = None
        workflow_id = parent_id
        # Newly-submitted parents are initially represented by their dashboard
        # action id. Resolve it to the Argo workflow id as soon as it is known.
        if parent_id.startswith("action:"):
            action = admin_repository.get(parent_id.removeprefix("action:"))
            if action:
                workflow_id = str(action.get("workflow_id") or parent_id)
                if not workflow_id.startswith("action:"):
                    # Persist this immediately: dashboard action state may be
                    # lost on restart, while the control record must survive.
                    record = control_store.link_parent_workflow(machine_id, int(record["month_index"]), workflow_id)
            else:
                # A temporary action id with no persisted action cannot prove a
                # live Argo workflow after restart. Do not present it as live.
                control_store.complete_action(machine_id, int(record["month_index"]), "completed")
                continue
        phase = str((action or {}).get("status") or "").lower()
        if not RUNTIME_INFO.workflow_mutations:
            # Avoid querying the developer's local Argo context in hosted
            # runtimes. The durable local control record is still useful, but
            # is reported without claiming remote workflow liveness.
            months.append(record)
            continue
        argo_confirmed_active = False
        if workflow_id and not workflow_id.startswith("action:"):
            live = running_workflows.lookup_workflows([workflow_id])
            if live:
                phase = str(live[0].get("status") or phase).lower()
                admin_repository.sync_workflow_statuses(live)
                argo_confirmed_active = phase not in terminal_phases
        if phase in terminal_phases:
            # The control record is an ownership reservation, not a historical
            # assertion that a terminated parent is still running.
            terminal_state = "failed" if phase in {"failed", "error", "terminated"} else "completed"
            control_store.complete_action(machine_id, int(record["month_index"]), terminal_state)
            continue
        # A durable control record is intentionally not enough to call a
        # submitted Argo workflow "running". Once it has a real workflow ID,
        # show it only when Argo positively confirms a non-terminal phase.
        # This prevents terminated/deleted workflows from being resurrected by
        # an old Azure JSON record after a dashboard restart.
        if workflow_id and not workflow_id.startswith("action:") and not argo_confirmed_active:
            continue
        if action:
            record = {**record, "parent_action_id": action.get("id"), "parent_workflow_id": workflow_id, "parent_status": phase or action.get("status")}
        months.append(record)
    return {"machine_id": machine_id, "months": months}


@app.get("/api/backfill/runner-lock-status")
def runner_lock_status() -> dict:
    """Return busy machines using one coalesced Argo read and active candidates.

    Combines two independent signals:
    1. Control-store records with an active state (dashboard-started backfills).
    2. Argo running workflows whose parameters reference a machine ID
       (catches backfills started outside the dashboard).

    Returns ``{busy_machines: {machine_id: {source, workflow_id?, months?}}}``
    so the runner table can disable busy rows without N individual fetches.
    """
    _release_stale_submission_reservations()
    if not RUNTIME_INFO.workflow_mutations:
        return {
            "busy_machines": {},
            "argo_available": False,
            "hint": _monitor_only_workflow_hint(),
        }
    busy: dict[str, dict] = {}
    terminal_phases = {"succeeded", "completed", "failed", "error", "terminated"}
    try:
        running_data = running_workflows.list_running()
    except Exception:
        running_data = {"workflows": []}
    candidates = {
        machine_id for action in admin_repository.list()
        if str(action.get("status", "")).lower() not in terminal_phases
        for machine_id in action.get("machine_ids", [])
    }
    for workflow in running_data.get("workflows", []):
        candidates.update(workflow.get("machine_ids") or admin_repository.machine_ids_for_workflow(workflow.get("workflow_id", "")))
    inventory_ids = {record.machine_id for record in control_plane.inventory.get_many(sorted(candidates))}

    for machine_id in inventory_ids:
        try:
            records = control_store.list_machine_states(machine_id)
        except Exception:
            continue
        active_months: list[dict] = []
        for record in records:
            if record.get("state") not in ACTIVE_MONTH_STATES:
                continue
            parent_id = str(record.get("parent_workflow_id") or "")
            workflow_id = parent_id
            if parent_id.startswith("action:"):
                action = admin_repository.get(parent_id.removeprefix("action:"))
                if action:
                    workflow_id = str(action.get("workflow_id") or parent_id)
                else:
                    continue
            live = next((item for item in running_data.get("workflows", []) if item.get("workflow_id") == workflow_id), None)
            if workflow_id and not workflow_id.startswith("action:") and not live:
                continue
            active_months.append(record)
        if active_months:
            busy[machine_id] = {"source": "control_store", "months": active_months}

    # Signal 2 — Argo running workflows (catches externally-started backfills).
    for workflow in running_data.get("workflows", []):
        phase = str(workflow.get("status") or "").lower()
        if phase in terminal_phases:
            continue
        wf_machine_ids = set(workflow.get("machine_ids") or admin_repository.machine_ids_for_workflow(workflow.get("workflow_id", "")))
        for mid in wf_machine_ids & inventory_ids:
            busy.setdefault(mid, {"source": "argo_running", "workflow_id": workflow.get("workflow_id"),
                                  "flow_name": workflow.get("flow_name"), "status": phase})

    return {"busy_machines": busy}


@app.post("/api/machines/{machine_id}/backfills/{month_index}/cancel")
def cancel_machine_month(machine_id: str, month_index: int, request: CancelMonthRequest, background_tasks: BackgroundTasks) -> dict:
    """Request a cooperative child cancellation and reserve safe replacements.

    The parent flow observes the marker; the dashboard never kills the parent
    merely to stop one month, preventing duplicate execution of later months.
    """
    try:
        current = control_store.get_month_state(machine_id, month_index)
        parent_id = request.parent_workflow_id.strip() or str((current or {}).get("parent_workflow_id") or "")
        if not parent_id:
            raise ValueError("An active parent workflow is required to cancel a month")
        if current and current.get("state") not in ACTIVE_MONTH_STATES:
            raise ValueError(f"Month is already {current.get('state')}; it cannot be cancelled")
        requeue_indices = sorted(set(request.requeue_month_indices) - {month_index})
        if requeue_indices:
            _reject_non_online_months({machine_id: requeue_indices})
        action_id = uuid4().hex
        record = control_store.request_cancel(machine_id, month_index, parent_id, request.child_workflow_id.strip(), request.requested_by)
        requeues = []
        for replacement_index in requeue_indices:
            requeues.append(control_store.mark_requeue_requested(machine_id, replacement_index, parent_id, request.requested_by, action_id=action_id))
        backfill_actions[action_id] = {
            "id": action_id,
            "status": "accepted",
            "machine_id": machine_id,
            "month_index": month_index,
            "parent_workflow_id": parent_id,
            "child_workflow_id": request.child_workflow_id.strip(),
            "requeue_month_indices": [item["month_index"] for item in requeues],
            "events": [
                {"state": "started", "at": datetime.now().isoformat()},
                {"state": "accepted", "at": datetime.now().isoformat()},
                *([{ "state": "waiting", "at": datetime.now().isoformat(), "detail": "Replacement remains blocked until the parent is terminal." }] if requeues else []),
            ],
        }
        if requeues:
            background_tasks.add_task(
                _wait_and_submit_replacements,
                action_id,
                machine_id,
                parent_id,
                [item["month_index"] for item in requeues],
                request.environment,
            )
        return {"action_id": action_id, "month": record, "requeue_months": requeues}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/backfill-actions/{action_id}")
def get_backfill_action(action_id: str) -> dict:
    action = backfill_actions.get(action_id)
    if not action:
        raise HTTPException(status_code=404, detail=f"Backfill action not found: {action_id}")
    return {"action": action}


@app.get("/api/admin/actions")
def list_admin_actions(machine_id: str | None = None) -> dict:
    if not RUNTIME_INFO.workflow_mutations:
        actions = admin_repository.for_machine(machine_id) if machine_id else admin_repository.list()
        return {"actions": actions, "confirmed_running_workflow_ids": [], "live_workflow_statuses": {}, "workflow_status_available": False}
    workflow_snapshot = running_workflows.list_recent()
    recent_workflows = workflow_snapshot.get("workflows", [])
    live_workflows = running_workflows.lookup_workflows(
        admin_repository.active_workflow_ids(machine_id)
    )
    # Prefer the exact lookup; it is the authoritative phase for every action
    # that the dashboard still considers active.
    workflows_by_id = {
        str(workflow.get("workflow_id", "")): workflow
        for workflow in [*recent_workflows, *live_workflows]
    }
    admin_repository.sync_workflow_statuses(list(workflows_by_id.values()))
    actions = admin_repository.for_machine(machine_id) if machine_id else admin_repository.list()
    recorded_workflow_ids = {action.get("workflow_id") for action in actions}
    for workflow in workflow_snapshot.get("workflows", []):
        workflow_machine_ids = workflow.get("machine_ids") or admin_repository.machine_ids_for_workflow(workflow.get("workflow_id", ""))
        if machine_id and machine_id not in workflow_machine_ids:
            continue
        if workflow.get("workflow_id") in recorded_workflow_ids:
            continue
        actions.append({
            "id": f"workflow-{workflow.get('workflow_id')}", "action": "trigger", "status": _action_status_from_workflow(workflow.get("status", "")),
            "command": [], "cwd": "", "created_at": workflow.get("created_at") or "", "finished_at": workflow.get("finished_at"),
            "stdout": "", "stderr": "", "outerbounds_url": workflow.get("outerbounds_url"), "workflow_id": workflow.get("workflow_id"),
            "flow_name": workflow.get("flow_name"), "machine_ids": workflow_machine_ids,
        })
    terminal_phases = {"succeeded", "completed", "failed", "error"}
    confirmed_running_workflow_ids = [
        workflow_id
        for workflow_id, workflow in workflows_by_id.items()
        if workflow_id
        and str(workflow.get("status", "")).lower() not in terminal_phases
    ]
    live_workflow_statuses = {
        workflow_id: str(workflow.get("status", ""))
        for workflow_id, workflow in workflows_by_id.items()
        if workflow_id and workflow_id in confirmed_running_workflow_ids
    }
    return {
        "actions": actions,
        # A persisted dashboard action is only a submission record. The machine
        # Running tab must use this live Argo-derived set, not the stored
        # `running` flag, which can survive a lost status lookup.
        "confirmed_running_workflow_ids": confirmed_running_workflow_ids,
        "live_workflow_statuses": live_workflow_statuses,
    }


@app.get("/api/admin/workflows/running")
def list_running_workflows() -> dict:
    if not RUNTIME_INFO.workflow_mutations:
        return {"workflows": [], "argo_available": False, "hint": _monitor_only_workflow_hint()}
    return running_workflows.list_running()


@app.get("/api/admin/workflows/summary")
def workflow_summary() -> dict:
    if not RUNTIME_INFO.workflow_mutations:
        return {"workflows": [], "counts": {}, "argo_available": False, "workflow_status_available": False,
                "hint": _monitor_only_workflow_hint(),
                "reviewed_workflow_ids": workflow_reviews.reviewed_ids()}
    summary = running_workflows.list_recent()
    admin_repository.sync_workflow_statuses(summary.get("workflows", []))
    for workflow in summary.get("workflows", []):
        workflow["machine_ids"] = workflow.get("machine_ids") or admin_repository.machine_ids_for_workflow(workflow.get("workflow_id", ""))
    summary["reviewed_workflow_ids"] = workflow_reviews.reviewed_ids()
    return summary


@app.websocket("/api/admin/workflows/live")
async def live_workflow_updates(websocket: WebSocket) -> None:
    """One socket per Live Operations view; Argo reads remain coalesced by the provider cache."""
    await websocket.accept()
    if not RUNTIME_INFO.workflow_mutations:
        await websocket.send_json({"workflows": [], "counts": {}, "argo_available": False,
                                   "hint": f"Workflow operations are disabled in {RUNTIME_INFO.mode} runtime; open Outerbounds for live status."})
        await websocket.close(code=1000)
        return
    try:
        while True:
            summary = await asyncio.to_thread(running_workflows.list_recent)
            for workflow in summary.get("workflows", []):
                workflow["machine_ids"] = workflow.get("machine_ids") or admin_repository.machine_ids_for_workflow(workflow.get("workflow_id", ""))
            summary["reviewed_workflow_ids"] = workflow_reviews.reviewed_ids()
            await websocket.send_json(summary)
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        return


@app.post("/api/admin/workflows/{workflow_id}/review")
def mark_workflow_reviewed(workflow_id: str) -> dict:
    try:
        return {"reviewed_workflow_ids": workflow_reviews.mark_reviewed(workflow_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/admin/workflows/{workflow_id}/review")
def clear_workflow_review(workflow_id: str) -> dict:
    try:
        return {"reviewed_workflow_ids": workflow_reviews.clear_reviewed(workflow_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/readiness")
def get_admin_readiness() -> dict:
    try:
        manifest_storage = manifest_writer.readiness()
    except Exception as exc:
        manifest_storage = {"ready": False, "detail": str(exc)}
    if RUNTIME_INFO.workflow_mutations:
        try:
            fullrlbl_test = command_builder.fullrlbl_test_readiness()
        except Exception as exc:
            fullrlbl_test = {"ready": False, "detail": str(exc)}
        argo = command_builder.ulrpm_orchestrator_readiness()
    else:
        unavailable = f"Workflow operations are disabled in {RUNTIME_INFO.mode} runtime."
        fullrlbl_test = {"ready": False, "detail": unavailable}
        argo = {"ready": False, "detail": unavailable}
    return {
        "manifest_storage": manifest_storage,
        "argo": argo,
        "fullrlbl_test": fullrlbl_test,
    }


def _require_argo_access() -> None:
    readiness = command_builder.platform_readiness()
    if not readiness["ready"]:
        raise RuntimeError(str(readiness["detail"]))


def _require_ulrpm_orchestrator_access() -> None:
    readiness = command_builder.ulrpm_orchestrator_readiness()
    if not readiness["ready"]:
        raise RuntimeError(str(readiness["detail"]))


@app.post("/api/admin/workflows/preflight")
def preflight_workflow(request: CreateWorkflowRequest) -> dict:
    source = WorkflowSourceRequest(**request.source.model_dump())
    try:
        return command_builder.validate_cli(source)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/admin/actions/{action_id}")
def get_admin_action(action_id: str) -> dict:
    action = admin_repository.get(action_id)
    if not action:
        raise HTTPException(status_code=404, detail=f"Admin action not found: {action_id}")
    return {"action": action}


@app.post("/api/admin/workflows/create")
def create_workflow(request: CreateWorkflowRequest, background_tasks: BackgroundTasks) -> dict:
    source = WorkflowSourceRequest(**request.source.model_dump())
    try:
        command_builder.validate_cli(source)
        _require_argo_access()
        cwd, command = command_builder.build_create(source)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    action = admin_repository.create(action="create", command=command, cwd=cwd, outerbounds_url=OUTERBOUNDS_RUNS_URL)
    background_tasks.add_task(admin_repository.run, action.id)
    return {"action": asdict(action)}


@app.post("/api/admin/manifests/machine")
def create_machine_manifest(request: MachineManifestRequestModel) -> dict:
    try:
        manifest = manifest_writer.write_machine_manifest(MachineManifestRequest(**request.model_dump()))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    action = admin_repository.record_completed(
        action="manifest",
        command=manifest_command_preview(manifest),
        cwd=DEFAULT_FLOW_PATH.parent,
        stdout=manifest_stdout(manifest),
        outerbounds_url=OUTERBOUNDS_RUNS_URL,
        machine_ids=[request.machine_id],
    )
    return {"manifest": asdict(manifest), "action": asdict(action)}


@app.post("/api/admin/manifests/multiple")
def create_multi_machine_manifest(request: MultiMachineManifestRequestModel) -> dict:
    try:
        manifest = manifest_writer.write_multi_machine_manifest(
            MultiMachineManifestRequest(**request.model_dump())
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    action = admin_repository.record_completed(
        action="manifest",
        command=manifest_command_preview(manifest),
        cwd=DEFAULT_FLOW_PATH.parent,
        stdout=manifest_stdout(manifest),
        outerbounds_url=OUTERBOUNDS_RUNS_URL,
        machine_ids=request.machine_ids,
    )
    return {"manifest": asdict(manifest), "action": asdict(action)}


@app.post("/api/admin/manifests/orchestrated")
def create_orchestrated_manifests(request: OrchestratedManifestRequest) -> dict:
    """Upload the monthly manifests consumed by the ULRPM parent without triggering it."""
    try:
        selected_indices = _selected_month_indices(
            request.machine_ids, request.since, request.until, request.month_indices_by_machine
        )
        _reject_non_online_months(selected_indices)
        manifest = manifest_writer.write_orchestrated_monthly_manifests(
            machine_ids=request.machine_ids,
            since=request.since,
            until=request.until,
            manifest_prefix=request.manifest_prefix,
            month_indices_by_machine=selected_indices,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    action = admin_repository.record_completed(
        action="manifest",
        command=["upload-orchestrator-monthly-manifests", manifest.manifest_prefix],
        cwd=DEFAULT_FLOW_PATH.parent,
        stdout=manifest_stdout(manifest),
        outerbounds_url="https://ui.augury.obp.outerbounds.com/dashboard/runs/p/default?flow_id=UlrpmDevBackfillOrchestratorFlow",
        machine_ids=manifest.machine_ids,
        flow_name="UlrpmDevBackfillOrchestratorFlow",
    )
    return {
        "manifest": _orchestrated_manifest_payload(manifest),
        "action": asdict(action),
        "machine_count": len(manifest.machine_ids),
        "month_count": manifest.month_count,
    }


@app.post("/api/admin/workflows/trigger")
def trigger_workflow(request: TriggerWorkflowRequest, background_tasks: BackgroundTasks) -> dict:
    source = WorkflowSourceRequest(**request.source.model_dump())
    try:
        _require_argo_access()
        params = TriggerParams(**request.params.model_dump())
        cwd, command = command_builder.build_trigger(source, params)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    action = admin_repository.create(action="trigger", command=command, cwd=cwd, outerbounds_url=OUTERBOUNDS_RUNS_URL)
    background_tasks.add_task(admin_repository.run, action.id)
    return {"action": asdict(action)}


@app.post("/api/admin/backfills/all", status_code=202)
def trigger_all_inventory_backfill(
    request: AllMachinesBackfillRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """Queue all-inventory preparation; return before any remote readiness or blob work."""
    try:
        params = TriggerParams(**request.params.model_dump())
        _validate_trigger_params_for_queue(params)
        if not request.month_indices_by_machine:
            start = validate_timestamp(request.since, field_name="since")
            end = validate_timestamp(request.until, field_name="until")
            if end <= start:
                raise ValueError("until must be after since")
        if request.manifest_path.strip():
            from .manifests import validate_manifest_prefix
            validate_manifest_prefix(request.manifest_path)
        action = admin_repository.create(
            action="trigger", command=[], cwd=DEFAULT_FLOW_PATH.parent,
            outerbounds_url=OUTERBOUNDS_RUNS_URL, machine_ids=[],
            flow_name="UlrpmDevBackfillOrchestratorFlow", phase="queued",
            exclusive_trigger_submission=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(_prepare_all_inventory_trigger, action.id, request.model_dump())
    return {"action": asdict(action), "accepted": True}


@app.post("/api/admin/backfills/orchestrated", status_code=202)
def trigger_orchestrated_machine_backfill(
    request: OrchestratedBackfillRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    try:
        params = TriggerParams(**request.params.model_dump())
        _validate_trigger_params_for_queue(params)
        machine_ids = validate_machine_ids(request.machine_ids)
        if params.max_parallel_steps > len(machine_ids):
            raise ValueError(f"Machine lane concurrency cannot exceed selected machine count ({len(machine_ids)})")
        selected_indices = _selected_month_indices(
            machine_ids, request.since, request.until, request.month_indices_by_machine
        )
        _validate_selected_indices_for_queue(selected_indices)
        request = request.model_copy(update={"machine_ids": machine_ids})
        action = admin_repository.create(
            action="trigger", command=[], cwd=DEFAULT_FLOW_PATH.parent,
            outerbounds_url=OUTERBOUNDS_RUNS_URL, machine_ids=machine_ids,
            flow_name="UlrpmDevBackfillOrchestratorFlow", phase="queued",
            exclusive_trigger_submission=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(_prepare_orchestrated_trigger, action.id, request.model_dump())
    return {"action": asdict(action), "accepted": True}


@app.get("/api/admin/fullrlbl/readiness")
def get_fullrlbl_readiness() -> dict:
    if not RUNTIME_INFO.workflow_mutations:
        return {"ready": False, "detail": f"Workflow operations are disabled in {RUNTIME_INFO.mode} runtime."}
    return command_builder.fullrlbl_test_readiness()


@app.get("/api/admin/fullrlbl/machines")
def get_fullrlbl_machines() -> dict:
    return {"machine_ids": scanner.inventory.list_machine_ids()}


@app.post("/api/admin/fullrlbl/trigger")
def trigger_fullrlbl_test(request: FullRlblTestRequestModel, background_tasks: BackgroundTasks) -> dict:
    try:
        readiness = command_builder.fullrlbl_test_readiness()
        if not readiness["ready"]:
            raise RuntimeError(str(readiness["detail"]))
        machine_ids = request.machine_ids
        if not machine_ids:
            machine_ids = scanner.inventory.list_machine_ids()
        params = FullRlblTestParams(
            machine_ids=machine_ids,
            fst_namespace=request.fst_namespace,
            lst_namespace=request.lst_namespace,
            pipeline_name=request.pipeline_name,
            manifest_path=request.manifest_path,
            runtime_patch=request.runtime_patch,
            persist_dev_lst=request.persist_dev_lst,
            seed_dev_lst=request.seed_dev_lst,
            feature_fetch_mode=request.feature_fetch_mode,
            test_mode=request.test_mode,
            wide_range_since=request.wide_range_since,
            wide_range_until=request.wide_range_until,
            memory_mb=request.memory_mb,
        )
        cwd, command = command_builder.build_fullrlbl_test_trigger(params)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    action = admin_repository.create(
        action="trigger",
        command=command,
        cwd=cwd,
        outerbounds_url=OUTERBOUNDS_FULLRLBL_TEST_URL,
        machine_ids=machine_ids,
        flow_name="FullRlblDevFstTestFlow",
    )
    background_tasks.add_task(admin_repository.run, action.id)
    return {
        "action": asdict(action),
        "machine_count": len(machine_ids),
        "test_mode": request.test_mode,
        "fst_namespace": request.fst_namespace,
    }


@app.post("/api/admin/workflows/terminate")
def terminate_workflow(request: TerminateWorkflowRequest, background_tasks: BackgroundTasks) -> dict:
    params = TerminateParams(**request.params.model_dump())
    try:
        _require_argo_access()
        cwd, command = command_builder.build_terminate(params)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    action = admin_repository.create(action="terminate", command=command, cwd=cwd, outerbounds_url=OUTERBOUNDS_RUNS_URL)
    background_tasks.add_task(admin_repository.run, action.id)
    return {"action": asdict(action)}


@app.post("/api/admin/logs")
def lookup_logs(request: LogsRequest, background_tasks: BackgroundTasks) -> dict:
    source = WorkflowSourceRequest(**request.source.model_dump())
    params = LogLookupParams(run_task_path=request.run_task_path, stream=request.stream)  # type: ignore[arg-type]
    try:
        cwd, command = command_builder.build_logs(source, params)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    action = admin_repository.create(action="logs", command=command, cwd=cwd, outerbounds_url=OUTERBOUNDS_RUNS_URL)
    background_tasks.add_task(admin_repository.run, action.id)
    return {"action": asdict(action)}


def _month_bounds(month: dict) -> tuple[str, str]:
    partition = month["partition"]
    year = int(partition["year"])
    month_value = int(partition["month"])
    since = datetime(year, month_value, 1)
    if month_value == 12:
        until = datetime(year + 1, 1, 1)
    else:
        until = datetime(year, month_value + 1, 1)
    return since.strftime("%Y/%m/%d/%H"), until.strftime("%Y/%m/%d/%H")


def _action_status_from_workflow(status: str) -> str:
    phase = status.lower()
    if phase in {"failed", "error"}:
        return "failed"
    if phase in {"succeeded", "completed"}:
        return "succeeded"
    return "running"


def _orchestrated_manifest_payload(manifest) -> dict:
    return {
        "account_name": manifest.account_name,
        "container_name": manifest.container_name,
        "manifest_path": manifest.manifest_template,
        "manifest_prefix": manifest.manifest_prefix,
        "rows": manifest.manifest_count,
        "blob_url": manifest.blob_url,
    }


def _selected_month_indices(
    machine_ids: list[str],
    since: str,
    until: str,
    requested: dict[str, list[int]],
) -> dict[str, list[int]]:
    """Resolve the complete selection before any manifest upload takes place."""
    if requested:
        if set(requested) != set(machine_ids):
            raise ValueError("month_indices_by_machine must contain exactly the requested machine IDs")
        return requested
    start = validate_timestamp(since, field_name="since")
    end = validate_timestamp(until, field_name="until")
    if end <= start:
        raise ValueError("until must be after since")
    indices = [index for index, *_ in orchestrator_month_windows(start, end)]
    return {machine_id: indices for machine_id in machine_ids}


def _validate_trigger_params_for_queue(params: TriggerParams) -> None:
    """Perform local-only trigger validation before accepting background work."""
    if params.environment not in {"dev", "prod"}:
        raise ValueError("environment must be dev or prod")
    if params.force_sessions_from_bucket:
        raise ValueError("force_sessions_from_bucket is not supported by the ULRPM parent orchestrator.")
    if params.max_parallel_steps < 1:
        raise ValueError("Machine lane concurrency must be >= 1")
    namespace = PROD_NAMESPACE if params.environment == "prod" else params.namespace.strip()
    if not namespace:
        raise ValueError("namespace is required")
    if params.environment == "prod" and (
        not params.confirm_production or params.confirmation_text != PROD_CONFIRMATION
    ):
        raise ValueError(f"Production trigger requires confirmation text: {PROD_CONFIRMATION}")
    if params.environment == "dev" and namespace == PROD_NAMESPACE:
        raise ValueError("Dev triggers cannot target feature-store-container")


def _validate_selected_indices_for_queue(month_indices_by_machine: dict[str, list[int]]) -> None:
    if not month_indices_by_machine:
        raise ValueError("Select at least one machine and month before starting a backfill")
    for machine_id, indices in month_indices_by_machine.items():
        if not indices:
            raise ValueError(f"Select at least one month for machine {machine_id}")
        if any(not isinstance(index, int) or index < 0 for index in indices):
            raise ValueError(f"Month indices for machine {machine_id} must be non-negative integers")
        if len(set(indices)) != len(indices):
            raise ValueError(f"Month indices for machine {machine_id} cannot contain duplicates")


def _fail_trigger_preparation(
    action_id: str,
    error: Exception | str,
    month_indices_by_machine: dict[str, list[int]] | None = None,
    *,
    reservations_attempted: bool = False,
) -> None:
    """Record preparation failure and release any reservations owned by this action."""
    action = admin_repository.get(action_id) or {}
    if action.get("phase") in {"accepted", "failed"}:
        return
    message = str(error)
    cleanup_pending = False
    if reservations_attempted and month_indices_by_machine:
        admin_repository.mark_failed(action_id, message, reservation_cleanup_pending=True)
        for machine_id, indices in month_indices_by_machine.items():
            for month_index in indices:
                try:
                    current = control_store.get_month_state(machine_id, month_index)
                    if current and current.get("parent_workflow_id") == f"action:{action_id}":
                        control_store.complete_action(machine_id, month_index, "failed", error=message)
                except Exception:
                    cleanup_pending = True
        if cleanup_pending:
            return
        admin_repository.mark_reservation_cleanup_complete(action_id)
        return
    admin_repository.mark_failed(action_id, message)


def _prepare_orchestrated_trigger(action_id: str, payload: dict) -> None:
    """Run the potentially slow validation, manifest, reservation and submit stages."""
    selected_indices: dict[str, list[int]] | None = None
    reservations_attempted = False
    try:
        admin_repository.set_trigger_phase(action_id, "validating")
        _require_ulrpm_orchestrator_access()
        request = OrchestratedBackfillRequest.model_validate(payload)
        params = TriggerParams(**request.params.model_dump())
        _validate_trigger_params_for_queue(params)
        selected_indices = _selected_month_indices(
            request.machine_ids, request.since, request.until, request.month_indices_by_machine
        )
        _validate_selected_indices_for_queue(selected_indices)
        _reject_non_online_months(selected_indices)

        admin_repository.set_trigger_phase(action_id, "preparing_manifests")
        manifest = manifest_writer.write_orchestrated_monthly_manifests(
            machine_ids=request.machine_ids,
            since=request.since,
            until=request.until,
            manifest_prefix=request.manifest_prefix,
            month_indices_by_machine=selected_indices,
        )
        _reject_active_months(manifest.month_indices_by_machine)
        cwd, command = command_builder.build_ulrpm_orchestrator_trigger(
            machine_ids=manifest.machine_ids,
            manifest_template=manifest.manifest_template,
            start_index=manifest.start_index,
            end_index=manifest.end_index,
            month_indices_by_machine=manifest.month_indices_by_machine,
            params=params,
        )
        admin_repository.record_completed(
            action="manifest",
            command=["upload-orchestrator-monthly-manifests", manifest.manifest_prefix],
            cwd=DEFAULT_FLOW_PATH.parent,
            stdout=manifest_stdout(manifest),
            outerbounds_url="https://ui.augury.obp.outerbounds.com/dashboard/runs/p/default?flow_id=UlrpmDevBackfillOrchestratorFlow",
            machine_ids=manifest.machine_ids,
            flow_name="UlrpmDevBackfillOrchestratorFlow",
        )
        admin_repository.update_trigger(
            action_id, phase="submitting", command=command, cwd=cwd,
            machine_ids=manifest.machine_ids,
        )
        _reserve_months_for_action(manifest.month_indices_by_machine, action_id)
        reservations_attempted = True
        _run_trigger_and_reconcile_reservations(action_id, manifest.month_indices_by_machine)
    except Exception as exc:
        _fail_trigger_preparation(
            action_id, exc, selected_indices,
            reservations_attempted=reservations_attempted,
        )


def _prepare_all_inventory_trigger(action_id: str, payload: dict) -> None:
    """Resolve and queue the legacy all-inventory selection in the background."""
    selected_indices: dict[str, list[int]] | None = None
    reservations_attempted = False
    try:
        admin_repository.set_trigger_phase(action_id, "validating")
        _require_ulrpm_orchestrator_access()
        request = AllMachinesBackfillRequest.model_validate(payload)
        params = TriggerParams(**request.params.model_dump())
        _validate_trigger_params_for_queue(params)
        query = MachineSearchQuery(limit=1, cohort="ulrpm", eligible=True)
        if (control_plane.inventory.search(query).total_estimate or 0) > 500:
            raise ValueError("Legacy all-inventory manifests are capped at 500 ULRPM machines; use a signed campaign estimate")
        selection = {"inventory_version": control_plane.inventory.get_version(), "filter": {"cohort": "ulrpm", "eligible": True}}
        machine_ids = [record.machine_id for record in iter_selection(control_plane.inventory, selection)]
        machine_ids = validate_machine_ids(machine_ids)
        if params.max_parallel_steps > len(machine_ids):
            raise ValueError(f"Machine lane concurrency cannot exceed selected machine count ({len(machine_ids)})")
        selected_indices = _selected_month_indices(
            machine_ids, request.since, request.until, request.month_indices_by_machine
        )
        _validate_selected_indices_for_queue(selected_indices)
        _reject_non_online_months(selected_indices)

        admin_repository.set_trigger_phase(action_id, "preparing_manifests")
        manifest = manifest_writer.write_orchestrated_monthly_manifests(
            machine_ids=machine_ids,
            since=request.since,
            until=request.until,
            manifest_prefix=request.manifest_path,
            month_indices_by_machine=selected_indices,
        )
        _reject_active_months(manifest.month_indices_by_machine)
        cwd, command = command_builder.build_ulrpm_orchestrator_trigger(
            machine_ids=manifest.machine_ids,
            manifest_template=manifest.manifest_template,
            start_index=manifest.start_index,
            end_index=manifest.end_index,
            month_indices_by_machine=manifest.month_indices_by_machine,
            params=params,
        )
        admin_repository.record_completed(
            action="manifest",
            command=["upload-orchestrator-monthly-manifests", manifest.manifest_prefix],
            cwd=DEFAULT_FLOW_PATH.parent,
            stdout=manifest_stdout(manifest),
            outerbounds_url="https://ui.augury.obp.outerbounds.com/dashboard/runs/p/default?flow_id=UlrpmDevBackfillOrchestratorFlow",
            machine_ids=manifest.machine_ids,
            flow_name="UlrpmDevBackfillOrchestratorFlow",
        )
        admin_repository.update_trigger(
            action_id, phase="submitting", command=command, cwd=cwd,
            machine_ids=manifest.machine_ids,
        )
        _reserve_months_for_action(manifest.month_indices_by_machine, action_id)
        reservations_attempted = True
        _run_trigger_and_reconcile_reservations(action_id, manifest.month_indices_by_machine)
    except Exception as exc:
        _fail_trigger_preparation(
            action_id, exc, selected_indices,
            reservations_attempted=reservations_attempted,
        )


def _reject_non_online_months(month_indices_by_machine: dict[str, list[int]]) -> None:
    """Fail closed unless the latest scan proves every selected month online."""
    machines = {
        machine.get("machine_id"): machine
        for machine in repository.latest()["snapshot"].get("machines", [])
    }
    rejected: list[str] = []
    for machine_id, indices in month_indices_by_machine.items():
        machine = machines.get(machine_id)
        indexed_months = {}
        if machine:
            for candidate in machine.get("months", []):
                partition = candidate.get("partition", {})
                try:
                    index = orchestrator_month_index(int(partition["year"]), int(partition["month"]))
                except (KeyError, TypeError, ValueError):
                    continue
                indexed_months[index] = candidate
        for month_index in indices:
            try:
                year, month_number = month_for_index(month_index)
            except (TypeError, ValueError):
                rejected.append(f"{machine_id}:month-index-{month_index} (activity unknown)")
                continue
            label = f"{year}-{month_number:02d}"
            candidate = indexed_months.get(month_index)
            activity = effective_activity_status(candidate) if candidate else "unknown"
            if not candidate or not is_month_backfillable(candidate):
                rejected.append(f"{machine_id}:{label} (activity {activity})")
    if rejected:
        raise ValueError(
            "Selected months are not backfillable; only online months can be submitted: "
            + ", ".join(rejected)
        )


def _reject_active_months(month_indices_by_machine: dict[str, list[int]]) -> None:
    """Enforce one parent backfill per machine, not merely per selected month."""
    _release_stale_submission_reservations()
    conflicts = []
    for machine_id, indices in month_indices_by_machine.items():
        # A ULRPM parent owns its whole machine lane. Allowing a different
        # month through while that parent is alive can create two writers for
        # the same FST partition sequence.
        active_records = [
            record for record in control_store.list_machine_states(machine_id)
            if record.get("state") in ACTIVE_MONTH_STATES
        ]
        if active_records:
            conflicts.extend(
                f"{machine_id}:{record['year']}-{record['month']:02d} ({record['state']})"
                for record in active_records
            )
            continue
        for month_index in indices:
            state = control_store.get_month_state(machine_id, month_index)
            if state and state.get("state") in ACTIVE_MONTH_STATES:
                conflicts.append(f"{machine_id}:{state['year']}-{state['month']:02d} ({state['state']})")
    if conflicts:
        raise ValueError("A selected month already has an active backfill: " + ", ".join(conflicts))


def _reserve_months_for_action(month_indices_by_machine: dict[str, list[int]], action_id: str) -> None:
    reserved: list[tuple[str, int]] = []
    try:
        for machine_id, indices in month_indices_by_machine.items():
            for month_index in indices:
                control_store.reserve_month(machine_id, month_index, f"action:{action_id}", "dashboard")
                reserved.append((machine_id, month_index))
    except Exception as exc:
        error = f"Reservation failed before submission: {exc}"
        admin_repository.mark_failed(action_id, error, reservation_cleanup_pending=True)
        for machine_id, month_index in reserved:
            control_store.complete_action(machine_id, month_index, "failed", error=error)
        admin_repository.mark_reservation_cleanup_complete(action_id)
        raise


def _run_trigger_and_reconcile_reservations(
    action_id: str,
    month_indices_by_machine: dict[str, list[int]],
) -> None:
    """Submit a parent and leave no action-owned reservation behind."""
    admin_repository.run(action_id)
    action = admin_repository.get(action_id) or {}
    workflow_id = str(action.get("workflow_id") or "")
    error = str(action.get("error") or "Argo submission did not return a workflow ID")
    cleanup_required = not (action.get("status") == "running" and workflow_id)
    if cleanup_required:
        admin_repository.mark_reservation_cleanup_pending(action_id)
    reconciliation_failed = False
    for machine_id, indices in month_indices_by_machine.items():
        for month_index in indices:
            try:
                current = control_store.get_month_state(machine_id, month_index)
                if not current or current.get("parent_workflow_id") != f"action:{action_id}":
                    continue
                if action.get("status") == "running" and workflow_id:
                    control_store.link_parent_workflow(machine_id, month_index, workflow_id)
                else:
                    control_store.complete_action(machine_id, month_index, "failed", error=error)
            except Exception:
                reconciliation_failed = True
    if cleanup_required and not reconciliation_failed:
        admin_repository.mark_reservation_cleanup_complete(action_id)
    elif reconciliation_failed:
        admin_repository.mark_reservation_cleanup_pending(action_id)


def _release_stale_submission_reservations() -> list[dict]:
    """Expire queued submissions that never entered the Argo command runner."""
    stale_after = int(os.getenv("BACKFILL_QUEUED_SUBMISSION_TTL_SECONDS", "1800"))
    stale_actions = admin_repository.fail_stale_queued_triggers(stale_after_seconds=stale_after)
    pending_actions = admin_repository.pending_reservation_cleanups()
    for action in pending_actions:
        action_id = str(action["id"])
        error = str(action.get("error") or "Submission expired before it started")
        cleanup_failed = False
        for machine_id in action.get("machine_ids", []):
            for record in control_store.list_machine_states(machine_id):
                if (
                    record.get("state") in ACTIVE_MONTH_STATES
                    and record.get("parent_workflow_id") == f"action:{action_id}"
                ):
                    try:
                        if action.get("status") == "running" and action.get("workflow_id"):
                            control_store.link_parent_workflow(
                                machine_id, int(record["month_index"]), str(action["workflow_id"])
                            )
                        else:
                            control_store.complete_action(
                                machine_id,
                                int(record["month_index"]),
                                "failed",
                                error=error,
                            )
                    except Exception:
                        cleanup_failed = True
        if not cleanup_failed:
            admin_repository.mark_reservation_cleanup_complete(action_id)
    return stale_actions


def _wait_and_submit_replacements(action_id: str, machine_id: str, parent_id: str, month_indices: list[int], environment: str = "dev") -> None:
    """Wait for the old parent to finish before submitting the replacement."""
    from .admin import PROD_NAMESPACE, PROD_CONFIRMATION, DEFAULT_DEV_NAMESPACE
    action = backfill_actions[action_id]
    action["status"] = "waiting"
    deadline = monotonic() + 24 * 60 * 60
    terminal = {"succeeded", "completed", "failed", "error", "terminated"}
    try:
        while monotonic() < deadline:
            if parent_id.startswith("action:"):
                parent_action = admin_repository.get(parent_id.removeprefix("action:")) or {}
                phase = str(parent_action.get("status") or "").lower()
            else:
                workflows = running_workflows.lookup_workflows([parent_id])
                phase = str(workflows[0].get("status") if workflows else "").lower()
            if phase in terminal:
                break
            time.sleep(5)
        else:
            raise RuntimeError("Timed out waiting for the cancelled parent workflow to become terminal")

        action["events"].append({"state": "replacement_submitted", "at": datetime.now().isoformat()})
        _reject_non_online_months({machine_id: month_indices})
        manifest = manifest_writer.write_orchestrated_monthly_manifests(
            machine_ids=[machine_id], since="", until="", month_indices_by_machine={machine_id: month_indices}
        )
        is_prod = environment == "prod"
        replacement_params = TriggerParams(
            environment=environment,
            namespace=PROD_NAMESPACE if is_prod else DEFAULT_DEV_NAMESPACE,
            max_parallel_steps=1,
            confirm_production=is_prod,
            confirmation_text=PROD_CONFIRMATION if is_prod else "",
        )
        cwd, command = command_builder.build_ulrpm_orchestrator_trigger(
            machine_ids=[machine_id], manifest_template=manifest.manifest_template,
            start_index=manifest.start_index, end_index=manifest.end_index,
            month_indices_by_machine=manifest.month_indices_by_machine,
            params=replacement_params,
        )
        replacement = admin_repository.create(
            action="trigger", command=command, cwd=cwd,
            outerbounds_url="https://ui.augury.obp.outerbounds.com/dashboard/runs/p/default?flow_id=UlrpmDevBackfillOrchestratorFlow",
            machine_ids=[machine_id], flow_name="UlrpmDevBackfillOrchestratorFlow",
        )
        admin_repository.run(replacement.id)
        replacement_id = replacement.workflow_id or f"action:{replacement.id}"
        for month_index in month_indices:
            control_store.complete_action(machine_id, month_index, "queued", replacement_workflow_id=replacement_id)
        action.update({"status": "success", "replacement_workflow_id": replacement_id})
        action["events"].append({"state": "success", "at": datetime.now().isoformat()})
    except Exception as exc:
        for month_index in month_indices:
            control_store.complete_action(machine_id, month_index, "failed", error=str(exc))
        action.update({"status": "failed", "error": str(exc)})
        action["events"].append({"state": "failure", "at": datetime.now().isoformat(), "detail": str(exc)})


# Register the SPA only after every API and WebSocket route above. An absent or
# incomplete frontend build intentionally leaves a backend-only FastAPI app.
register_static_spa_host(app)
