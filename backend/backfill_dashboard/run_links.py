"""Link FSTBackfill child runs to the ULRPM orchestrator parent that launched them.

Argo/Metaflow expose no parent id on a child run: the orchestrator submits each child
from the FSTBackfill WorkflowTemplate with only the flow parameters, so a child carries
no owner reference, label or ``triggered_by`` pointing at its parent (checked on
``fstbackfill.test.devfstmon.fstbackfill-mrqqu-kx9ds``). What both sides DO carry is the
manifest batch directory the dashboard wrote for that submission:

* parent ``manifest_template``:      ``<batch>/monthly_{machine_short}_{machine_id}/month_...parquet``
* child ``storage_account_manifest_path``: ``<batch>/monthly_<short>_<machine>/month_<ii>_<yyyy>_<mm>.parquet``
  (or ``.../month_<ii>_<yyyy>_<mm>/part_<nnn>_...parquet`` for sub-month splits)

``<batch>`` (e.g. ``2026/10/12/dashboard/orchestrator/090750_773688``) holds no machine id,
so the public GitHub Actions status artifact can carry it plus the month (index, year,
month). Machine ids are attached here, on the app side, from the parent's own record
(its admin action / its ``machine_ids`` parameter), never from the public artifact.
``scripts/ob_dispatch.py`` mirrors ``link_from_parameters`` (see the parity test).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping

_CHILD_RE = re.compile(
    r"^(?P<batch>.+?)/monthly_[^/]+/month_(?P<index>\d{1,4})_(?P<year>\d{4})_(?P<month>\d{2})(?:\.parquet$|/part_\d+_)"
)
_PARENT_RE = re.compile(r"^(?P<batch>.+?)/monthly_\{machine_short\}_\{machine_id\}/")


def _unquote(value: Any) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = json.loads(text)
            return decoded if isinstance(decoded, str) else text
        except ValueError:
            return text
    return text


def link_from_parameters(parameters: Mapping[str, Any]) -> dict | None:
    """Machine-free link fields from a run's flow parameters (Argo names)."""
    child = _CHILD_RE.match(_unquote(parameters.get("storage_account_manifest_path")))
    if child:
        return {"role": "child", "manifest_batch": child["batch"], "month_index": int(child["index"]),
                "year": int(child["year"]), "month": int(child["month"])}
    parent = _PARENT_RE.match(_unquote(parameters.get("manifest_template")))
    if parent:
        return {"role": "parent", "manifest_batch": parent["batch"]}
    return None


def argo_parameters(item: Mapping[str, Any]) -> dict[str, Any]:
    params = (((item.get("spec") or {}).get("arguments") or {}).get("parameters")) or []
    return {str(p.get("name")): p.get("value") for p in params if isinstance(p, dict) and p.get("name")}


def _action_params(action: Mapping[str, Any]) -> dict[str, Any]:
    """Trigger parameters from a GitHub Actions dispatch child command (``... '<json>'``)."""
    command = action.get("command") or []
    if not command:
        return {}
    try:
        payload = json.loads(command[-1])
    except (TypeError, ValueError):
        return {}
    params = ((payload or {}).get("inputs") or {}).get("params") if isinstance(payload, dict) else None
    return params if isinstance(params, dict) else {}


def _month_plan(params: Mapping[str, Any]) -> dict[str, set[int]]:
    raw = _unquote(params.get("machine_month_indices"))
    try:
        plan = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(plan, dict):
        return {}
    return {str(machine): {int(i) for i in indices if isinstance(i, int)} for machine, indices in plan.items()
            if isinstance(indices, list)}


def link_children(workflows: list[dict], actions: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Set ``parent_workflow_id`` / month / ``machine_ids`` on child rows and ``child_workflow_ids`` on parents.

    Mutates and returns ``workflows``. A child is linked only when its manifest batch matches
    exactly one known parent (a parent row in the snapshot or a dashboard trigger action).
    """
    parents: dict[str, dict] = {}
    for action in actions:
        if action.get("action") != "trigger" or not action.get("workflow_id"):
            continue
        params = _action_params(action)
        link = link_from_parameters(params)
        if link and link["role"] == "parent":
            parents.setdefault(link["manifest_batch"], {
                "workflow_id": str(action["workflow_id"]), "machine_ids": list(action.get("machine_ids") or []),
                "plan": _month_plan(params)})
    for workflow in workflows:
        if workflow.get("link_role") == "parent" and workflow.get("manifest_batch"):
            known = parents.setdefault(workflow["manifest_batch"], {
                "workflow_id": workflow["workflow_id"], "machine_ids": list(workflow.get("machine_ids") or []), "plan": {}})
            if not known["machine_ids"]:
                known["machine_ids"] = list(workflow.get("machine_ids") or [])
    by_id = {str(w.get("workflow_id")): w for w in workflows}
    for workflow in workflows:
        if workflow.get("link_role") != "child":
            continue
        parent = parents.get(str(workflow.get("manifest_batch") or ""))
        if not parent:
            continue
        workflow["parent_workflow_id"] = parent["workflow_id"]
        if not workflow.get("machine_ids"):
            month_index = workflow.get("month_index")
            plan = parent["plan"]
            candidates = [m for m in parent["machine_ids"] if not plan or month_index in plan.get(m, set())]
            workflow["machine_ids"] = candidates or list(parent["machine_ids"])
        row = by_id.get(parent["workflow_id"])
        if row is not None:
            children = row.setdefault("child_workflow_ids", [])
            if workflow["workflow_id"] not in children:
                children.append(workflow["workflow_id"])
    return workflows
