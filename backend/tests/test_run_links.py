"""Regression: Live Ops must show the FSTBackfill children of a ULRPM parent, linked (BUG 2)."""
from __future__ import annotations

import json
import sys
import types
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from backfill_dashboard import run_links
from backfill_dashboard.admin import parse_running_workflows

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import ob_dispatch  # noqa: E402

MACHINE = "67ed190c99a0ea11cd3dced5"
BATCH = "2026/10/12/dashboard/orchestrator/090750_773688"
PARENT = "fstbkfill.test.devfstm.ulrpmdeatorflow-hwqbr-s2skp"
CHILD = "fstbackfill.test.devfstmon.fstbackfill-mrqqu-kx9ds"
FST_DEPLOYMENT = "fstbackfill.test.devfstmon.fstbackfill-mrqqu"
ORCH_DEPLOYMENT = "fstbkfill.test.devfstm.ulrpmdeatorflow-hwqbr"
CHILD_PATH = f'"{BATCH}/monthly_67ed_{MACHINE}/month_08_2025_04.parquet"'  # Argo keeps the JSON quotes
TEMPLATE = f'"{BATCH}/monthly_{{machine_short}}_{{machine_id}}/month_{{month_index:02d}}_{{year}}_{{month:02d}}.parquet"'


def _argo(name, flow, params, phase="Running"):
    return {"metadata": {"name": name, "namespace": "jobs-default", "annotations": {"metaflow/flow_name": flow}},
            "status": {"phase": phase}, "spec": {"arguments": {"parameters": [{"name": k, "value": v} for k, v in params.items()]}}}


CASES = [
    ({"storage_account_manifest_path": CHILD_PATH},
     {"role": "child", "manifest_batch": BATCH, "month_index": 8, "year": 2025, "month": 4}),
    ({"storage_account_manifest_path": f"{BATCH}/monthly_67ed_{MACHINE}/month_08_2025_04/part_002_20250411_20250421.parquet"},
     {"role": "child", "manifest_batch": BATCH, "month_index": 8, "year": 2025, "month": 4}),
    ({"manifest_template": TEMPLATE}, {"role": "parent", "manifest_batch": BATCH}),
    ({"storage_account_manifest_path": "manual/run.parquet"}, None),
    ({}, None),
]


@pytest.mark.parametrize("params,expected", CASES)
def test_link_parsing_and_script_parity(params, expected):
    assert run_links.link_from_parameters(params) == expected
    assert ob_dispatch.run_link(params) == expected  # the public script emits the same fields
    if expected:
        assert MACHINE not in json.dumps(expected) and "67ed" not in json.dumps(expected)


def test_children_link_to_parent_and_inherit_the_planned_machine():
    rows = [_argo(PARENT, "UlrpmDevBackfillOrchestratorFlow", {"manifest_template": TEMPLATE}, "Failed"),
            _argo(CHILD, "FSTBackfill", {"storage_account_manifest_path": CHILD_PATH}, "Running"),
            _argo("fstbackfill-unrelated", "FSTBackfill", {"storage_account_manifest_path": '"other/monthly_x_y/month_01_2024_01.parquet"'})]
    # As served from the public snapshot: parameters stripped, only ``link`` kept.
    for row in rows:
        link = ob_dispatch.run_link({p["name"]: p["value"] for p in row["spec"]["arguments"]["parameters"]})
        row.pop("spec")
        if link:
            row["link"] = link
    workflows = [asdict(item) for item in parse_running_workflows(rows)]
    action = {"action": "trigger", "workflow_id": PARENT, "machine_ids": [MACHINE, "6817571193e37ef05fffcd1c"],
              "command": ["python", "-m", "x", json.dumps({"op": "trigger", "inputs": {"params": {
                  "manifest_template": TEMPLATE, "machine_month_indices": json.dumps({MACHINE: [8], "6817571193e37ef05fffcd1c": [9]})}}})]}
    run_links.link_children(workflows, [action])
    by_id = {w["workflow_id"]: w for w in workflows}
    child = by_id[CHILD]
    assert child["parent_workflow_id"] == PARENT and child["machine_ids"] == [MACHINE]
    assert (child["month_index"], child["year"], child["month"]) == (8, 2025, 4)
    assert by_id[PARENT]["child_workflow_ids"] == [CHILD]
    assert by_id["fstbackfill-unrelated"].get("parent_workflow_id") is None


def test_child_links_through_a_parent_row_without_an_action_record():
    workflows = [{"workflow_id": PARENT, "link_role": "parent", "manifest_batch": BATCH, "machine_ids": [MACHINE]},
                 {"workflow_id": CHILD, "link_role": "child", "manifest_batch": BATCH, "month_index": 8, "machine_ids": []}]
    run_links.link_children(workflows, [])
    assert workflows[1]["parent_workflow_id"] == PARENT and workflows[1]["machine_ids"] == [MACHINE]


# --- list_runs on the dispatch runner: deployment-scoped, children included -------------------

class _Run:
    def __init__(self, run_id, finished=True):
        self.id, self.finished, self.successful = run_id, finished, True
        self.created_at = self.finished_at = None


def test_list_runs_keeps_both_allowlisted_deployments_and_skips_crowding_runs(monkeypatch):
    other = [_Run(f"argo-fstbackfill.prod.someone.fstbackfill-zzz-{i:03d}") for i in range(30)]
    runs = {"FSTBackfill": other[:25] + [_Run(f"argo-{CHILD}", finished=False)] + other[25:],
            "UlrpmDevBackfillOrchestratorFlow": [_Run(f"argo-{PARENT}")]}
    metaflow = types.ModuleType("metaflow")
    metaflow.Flow = lambda name: SimpleNamespace(runs=lambda: iter(runs[name]))
    exception = types.ModuleType("metaflow.exception")
    exception.MetaflowNotFound = type("MetaflowNotFound", (Exception,), {})
    monkeypatch.setitem(sys.modules, "metaflow", metaflow)
    monkeypatch.setitem(sys.modules, "metaflow.exception", exception)
    argo = {CHILD: _argo(CHILD, "FSTBackfill", {"storage_account_manifest_path": CHILD_PATH}),
            PARENT: _argo(PARENT, "UlrpmDevBackfillOrchestratorFlow", {"manifest_template": TEMPLATE,
                                                                        "machine_ids": f'"{MACHINE}"'}, "Failed")}
    monkeypatch.setattr(ob_dispatch, "_argo_client", lambda: SimpleNamespace(get_workflow=lambda name: argo.get(name)))
    env = {"OB_DISPATCH_DEPLOYMENTS": f"{FST_DEPLOYMENT},{ORCH_DEPLOYMENT}"}
    result = ob_dispatch.op_list_runs({"flow": None, "limit": 20, "running_only": False}, env)
    names = [item["metadata"]["name"] for item in result["workflows"]]
    assert names == [CHILD, PARENT]  # 25 newer runs of other deployments no longer crowd the child out
    child = result["workflows"][0]
    assert child["link"]["manifest_batch"] == BATCH and child["link"]["month_index"] == 8
    assert MACHINE not in json.dumps(result)  # public artifact: no machine ids (params not included)
    parsed = {w.workflow_id: w for w in parse_running_workflows(result["workflows"])}
    assert parsed[CHILD].link_role == "child" and parsed[PARENT].link_role == "parent"
