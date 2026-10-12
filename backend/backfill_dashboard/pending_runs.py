"""Dashboard-submitted runs that the workflow status source has not reported yet.

On the GitHub Actions backend the status source is a cached ``list_runs`` snapshot that
is itself produced by a GitHub Actions run, so a freshly triggered workflow can take
minutes to appear. The dashboard already knows what it dispatched (admin action records:
machine ids, flow, workflow id once the trigger run returns), so Live Operations and the
machine views show those records immediately as *pending* rows and replace them with the
real status as soon as a snapshot contains the workflow. Machine ids come only from the
app's own records, never from the (public) GitHub artifacts.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .admin import _outerbounds_run_url

DEFAULT_WINDOW_SECONDS = 1800
_GITHUB_RUN_RE = re.compile(r"\((https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/\d+)\)")
_ACTIVE = {"queued", "running"}


def window_seconds(environ: Mapping[str, str] | None = None) -> int:
    raw = (environ if environ is not None else os.environ).get("OUTERBOUNDS_PENDING_WINDOW_SECONDS", "")
    try:
        return max(60, min(int(raw), 6 * 3600)) if raw else DEFAULT_WINDOW_SECONDS
    except ValueError:
        return DEFAULT_WINDOW_SECONDS


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _recent_dispatches(actions: Iterable[Mapping], now: datetime, window: int) -> list[Mapping]:
    recent = []
    for action in actions:
        if action.get("action") != "trigger" or action.get("status") not in _ACTIVE:
            continue
        created = _parse(action.get("created_at"))
        if created is None or (now - created).total_seconds() > window:
            continue
        recent.append(action)
    return recent


def _known(workflow_ids: Iterable[str]) -> set[str]:
    return {str(item).removeprefix("argo-") for item in workflow_ids if item}


def expected_workflow_ids(actions: Iterable[Mapping], known_ids: Iterable[str], *,
                          now: datetime | None = None, window: int = DEFAULT_WINDOW_SECONDS) -> list[str]:
    """Workflow ids the dashboard triggered recently that the status source has not shown yet."""
    now = now or datetime.now(timezone.utc)
    known = _known(known_ids)
    return [str(action["workflow_id"]) for action in _recent_dispatches(actions, now, window)
            if action.get("workflow_id") and str(action["workflow_id"]).removeprefix("argo-") not in known]


def pending_rows(actions: Iterable[Mapping], known_ids: Iterable[str], *,
                 now: datetime | None = None, window: int = DEFAULT_WINDOW_SECONDS) -> list[dict]:
    """RunningWorkflow-shaped rows for recent dashboard dispatches missing from the status source."""
    now = now or datetime.now(timezone.utc)
    known = _known(known_ids)
    rows = []
    for action in _recent_dispatches(actions, now, window):
        workflow_id = str(action.get("workflow_id") or "")
        if workflow_id and workflow_id.removeprefix("argo-") in known:
            continue
        flow = str(action.get("flow_name") or "")
        match = _GITHUB_RUN_RE.search(str(action.get("stdout") or ""))
        github_run_url = match.group(1) if match else None
        if workflow_id:
            status, detail = "Submitted", "Triggered; waiting for the first Outerbounds status snapshot."
            url = _outerbounds_run_url(flow, f"argo-{workflow_id.removeprefix('argo-')}") if flow else (action.get("outerbounds_url") or "")
        elif action.get("phase") == "cancel_window":
            status, detail, url = "Queued", "Queued; can still be cancelled before dispatch.", ""
        else:
            status, detail, url = "Dispatching", "Dispatching through GitHub Actions.", ""
        rows.append({
            "workflow_id": workflow_id or f"pending-{str(action.get('id', ''))[:12]}",
            "flow_name": flow or "Dashboard dispatch",
            "namespace": "",
            "status": status,
            "branch": None,
            "created_at": action.get("created_at"),
            "started_at": None,
            "finished_at": None,
            "outerbounds_url": url or github_run_url or "",
            "machine_ids": list(action.get("machine_ids") or []),
            "pending": True,
            "pending_detail": detail,
            "action_id": action.get("id"),
            "github_run_url": github_run_url,
        })
    return rows
