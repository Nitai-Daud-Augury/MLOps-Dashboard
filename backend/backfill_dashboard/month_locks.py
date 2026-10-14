"""Release durable month locks (control-store records) once their workflow is terminal.

A month record is reserved ``queued`` at submission and linked to the parent Argo id. The
ULRPM parent flow updates it while it runs, but a parent that is *terminated* (or crashes)
never writes a terminal state, and before this module only ``GET .../backfills/active``
released a lock, and only when the status source positively returned the workflow as
terminal. On the GitHub Actions backend a workflow that is absent from the latest
``list_runs`` snapshot was simply hidden there, so the record stayed ``queued`` and
``_reject_active_months`` kept refusing new backfills for that machine.

``decide`` is the single rule set, applied:

* right after a terminate succeeds (``terminated_ids``),
* on every status snapshot (GitHub Actions refresh) and before every submission guard,
  including workflows absent from the snapshot that the dashboard knows are terminal
  (a successful terminate, or a trigger action already synced to failed/succeeded),
* as a TTL safety net (``BACKFILL_MONTH_LOCK_STALE_TTL_SECONDS``) for locks whose
  workflow the latest snapshot does not show as active.

A workflow the latest status shows as active is never released, whatever its age.
``requeue_pending`` belongs to the replacement waiter and is only TTL-released.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping

from .control_store import ACTIVE_MONTH_STATES

# Argo phases (Succeeded/Failed/Error; a terminated workflow ends as Failed), Metaflow run
# state (Succeeded/Failed), GitHub Actions conclusions and the dashboard's own action statuses.
TERMINAL_PHASES = {"succeeded", "completed", "failed", "error", "terminated", "cancelled", "canceled", "aborted", "timed_out", "skipped"}


def is_terminal(status: object) -> bool:
    return str(status or "").strip().lower() in TERMINAL_PHASES
STALE_TTL_ENV = "BACKFILL_MONTH_LOCK_STALE_TTL_SECONDS"
DEFAULT_STALE_TTL_SECONDS = 24 * 3600
_MIN_TTL, _MAX_TTL = 900, 30 * 24 * 3600


def stale_ttl_seconds(environ: Mapping[str, str] | None = None) -> int:
    """TTL for the stale-lock safety net; ``0`` disables it. Clamped to [15 min, 30 days]."""
    raw = str((environ if environ is not None else os.environ).get(STALE_TTL_ENV, "")).strip()
    if not raw:
        return DEFAULT_STALE_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_STALE_TTL_SECONDS
    return 0 if value <= 0 else max(_MIN_TTL, min(value, _MAX_TTL))


def _norm(workflow_id: object) -> str:
    return str(workflow_id or "").strip().removeprefix("argo-")


def owner_workflow_id(record: Mapping) -> str:
    """The workflow that owns the lock: a replacement if one was submitted, else the parent."""
    return _norm(record.get("replacement_workflow_id") or record.get("parent_workflow_id"))


def record_workflow_ids(record: Mapping) -> list[str]:
    ids = [owner_workflow_id(record), _norm(record.get("parent_workflow_id")), _norm(record.get("child_workflow_id"))]
    return [item for i, item in enumerate(ids) if item and not item.startswith("action:") and item not in ids[:i]]


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Release:
    state: str  # completed | failed | cancelled
    reason: str
    source: str  # snapshot | terminate | known_terminal | stale_ttl | admin


def _state_for_phase(phase: str, terminated: bool) -> str:
    if terminated:
        return "cancelled"
    return "completed" if phase in {"succeeded", "completed"} else "failed"


def is_active_in(record: Mapping, workflows_by_id: Mapping[str, Mapping]) -> str | None:
    """The id of a workflow of this record that the status source shows as non-terminal."""
    for workflow_id in record_workflow_ids(record):
        workflow = workflows_by_id.get(workflow_id)
        if workflow is not None and str(workflow.get("status") or "").lower() not in TERMINAL_PHASES:
            return workflow_id
    return None


def decide(record: Mapping, *, workflows_by_id: Mapping[str, Mapping], terminated_ids: Iterable[str] = (),
           terminal_ids: Iterable[str] = (), now: datetime | None = None, ttl_seconds: int = 0) -> Release | None:
    """Whether (and how) to release one month record. ``None`` keeps it."""
    state = record.get("state")
    if state not in ACTIVE_MONTH_STATES:
        return None
    owner = owner_workflow_id(record)
    if not owner or owner.startswith("action:"):
        return None  # submission still in flight: _release_stale_submission_reservations owns it
    terminated = {_norm(item) for item in terminated_ids}
    # A successful terminate is newer than any cached snapshot that still shows the run active.
    live = {key: value for key, value in workflows_by_id.items()
            if key not in terminated or str(value.get("status") or "").lower() in TERMINAL_PHASES}
    if is_active_in(record, live):
        return None
    if state != "requeue_pending":
        workflow = workflows_by_id.get(owner)
        if workflow is not None:
            phase = str(workflow.get("status") or "").lower()
            return Release(_state_for_phase(phase, owner in terminated),
                           f"workflow {owner} is {workflow.get('status')} in the latest status snapshot", "snapshot")
        if owner in terminated:
            return Release("cancelled", f"workflow {owner} was terminated from the dashboard", "terminate")
        if owner in {_norm(item) for item in terminal_ids}:
            return Release("failed", f"workflow {owner} is terminal per the dashboard's action record "
                                     "(absent from the latest status snapshot)", "known_terminal")
    if ttl_seconds > 0:
        updated = _parse(record.get("updated_at") or record.get("created_at"))
        age = ((now or datetime.now(timezone.utc)) - updated).total_seconds() if updated else None
        if age is not None and age > ttl_seconds:
            return Release("failed", f"stale lock: no active workflow in the latest status for {int(age)}s "
                                     f"(> {STALE_TTL_ENV}={ttl_seconds})", "stale_ttl")
    return None


def release_records(store, records: Iterable[Mapping], *, workflows_by_id: Mapping[str, Mapping],
                    terminated_ids: Iterable[str] = (), terminal_ids: Iterable[str] = (), now: datetime | None = None,
                    ttl_seconds: int = 0, actor: str = "dashboard-reconciler",
                    log: Callable[[str], None] | None = None) -> list[dict]:
    """Apply ``decide`` to records and write the releases. Returns the updated records."""
    log = log or (lambda message: print(f"[month-locks] {message}", flush=True))
    terminated_ids, terminal_ids = list(terminated_ids), list(terminal_ids)
    released = []
    for record in records:
        release = decide(record, workflows_by_id=workflows_by_id, terminated_ids=terminated_ids,
                         terminal_ids=terminal_ids, now=now, ttl_seconds=ttl_seconds)
        if release is None:
            continue
        machine_id, month_index = str(record["machine_id"]), int(record["month_index"])
        try:
            updated = store.complete_action(machine_id, month_index, release.state, error=release.reason, audit={
                "actor": actor, "source": release.source, "released_from": record.get("state"),
                "workflow_id": owner_workflow_id(record)})
        except Exception as exc:  # noqa: BLE001 - one failed write must not block the others
            log(f"release failed for {machine_id}:{month_index}: {type(exc).__name__}: {exc}")
            continue
        log(f"released {machine_id}:{record.get('year')}-{int(record.get('month') or 0):02d} "
            f"{record.get('state')} -> {release.state} ({release.source}: {release.reason})")
        released.append(updated)
    return released


def workflows_index(workflows: Iterable[Mapping]) -> dict[str, Mapping]:
    return {_norm(item.get("workflow_id")): item for item in workflows if item.get("workflow_id")}


def action_terminal_sets(actions: Iterable[Mapping]) -> tuple[set[str], set[str]]:
    """(terminated workflow ids, otherwise-terminal workflow ids) from the admin action ledger."""
    terminated, terminal = set(), set()
    for action in actions:
        workflow_id = _norm(action.get("workflow_id"))
        if not workflow_id:
            continue
        if action.get("action") == "terminate" and action.get("status") == "succeeded":
            terminated.add(workflow_id)
        elif action.get("action") == "trigger" and action.get("status") in {"failed", "succeeded"} and action.get("phase") != "failed":
            # phase "failed" = the submission itself failed; such an action never owned a workflow.
            terminal.add(workflow_id)
    return terminated, terminal
