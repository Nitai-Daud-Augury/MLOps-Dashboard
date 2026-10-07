import ast
import copy
import datetime
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from sibling_repos import requires_flow_sources

pytestmark = requires_flow_sources

ROOT = Path(__file__).resolve().parents[3]
FLOW = ROOT / "Augury repos" / "metaflow-bx" / "FSTBackfill_prod_flow.py"
ORCHESTRATOR = (
    ROOT
    / "Augury repos"
    / "MLOps research"
    / "ulrpm_dev_backfill"
    / "UlrpmDevBackfillOrchestratorFlow.py"
)
BUNDLED_FLOW = ORCHESTRATOR.with_name("FSTBackfill_prod_flow.py")
BACKFILL_TRIGGER = FLOW.parent / "backfill_fst" / "backfill_fst_utils" / "triggers.py"


def test_flow_has_no_hardcoded_750_or_parallelism_alias():
    source = FLOW.read_text()
    assert "total_steps=750" not in source
    assert 'Parameter("max_parallel_steps"' not in source
    assert "manifest_bucket_count=self.manifest_bucket_count" in source


def test_production_namespace_skips_dev_feature_seeding():
    source = FLOW.read_text()
    assert 'self.is_prod = self.ns == "feature-store-container"' in source
    assert "if not self.is_prod:" in source
    assert "seed_dev_features" not in source


def test_orchestrator_accepts_and_validates_selected_namespace():
    source = ORCHESTRATOR.read_text()
    assert 'PROD_NAMESPACE = "feature-store-container"' in source
    assert "Refusing to run against production FST namespace" not in source
    assert 'f"Is prod: {name_space == PROD_NAMESPACE}"' in source
    assert '"--name-space",\n        name_space,' in source
    assert 'CHILD_BRANCH = "dev_fst_month_isolation"' in source
    assert "seed_dev_features" not in source


def test_orchestrator_packages_current_child_launcher_and_has_no_azure_control_io():
    source = ORCHESTRATOR.read_text()
    assert BUNDLED_FLOW.read_text() == FLOW.read_text()
    assert "validate_child_launcher_contract()" in source
    assert 'Path(__file__).resolve().with_name(FLOW_SCRIPT)' in source
    assert "--manifest_bucket_count" in source
    assert "DefaultAzureCredential" not in source
    assert "update_control_state" not in source
    assert "cancellation_requested" not in source
    assert "upload_batch_summary" not in source


def test_resource_wrappers_are_explicit_and_standard_has_no_pool_selector():
    source = FLOW.read_text()
    assert 'RESOURCE_MEMORY = "8192" if RESOURCE_PROFILE == "standard" else os.getenv("FST_BACKFILL_ULRPM_MEMORY", "300000")' in source
    assert 'FST_BACKFILL_ULRPM_MEMORY", "300000"' in source
    assert 'FST_BACKFILL_ULRPM_SAMPLE_MEMORY", "500000"' in source
    assert source.count('memory=RESOURCE_SAMPLE_MEMORY, node_selector=RESOURCE_NODE_SELECTOR') == 2
    assert source.count('memory=RESOURCE_MEMORY, node_selector=RESOURCE_NODE_SELECTOR') == 2
    assert 'if RESOURCE_PROFILE == "ulrpm" else {}' in source
    assert '"outerbounds.co/compute-pool": "obp-main-big3"' in source
    assert "three 64G-request heavy pods" in source
    assert "500000M (decimal MB), about 466 GiB" in source
    for name, profile in (("FSTBackfill_standard_flow.py", "standard"), ("FSTBackfill_ulrpm_flow.py", "ulrpm")):
        assert f'FST_BACKFILL_RESOURCE_PROFILE"] = "{profile}"' in (FLOW.parent / name).read_text()


def test_caught_step_failures_are_reported_and_fail_the_child_run():
    source = FLOW.read_text()
    assert "Skipping feature extraction because fetch_machine_samples failed" in source
    assert "Skipping anomaly features because an upstream step failed" in source
    assert '"failure_details": failure_details' in source
    assert "FST backfill failed in one or more caught steps" in source
    assert 'unsuccessful_machines.append("unknown")' not in source


def _flow_function(name):
    tree = ast.parse(FLOW.read_text())
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    definitions += [node for cls in tree.body if isinstance(cls, ast.ClassDef)
                    for node in cls.body if isinstance(node, ast.FunctionDef)]
    definition = copy.deepcopy(next(node for node in definitions if node.name == name))
    definition.decorator_list = []
    scope = {"datetime": datetime, "json": json}
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(FLOW), "exec"), scope)
    return scope[name]


def test_aggregate_report_tracks_failures_without_daily_fanout(monkeypatch):
    reports = []
    clients = ModuleType("mlops_operations.clients_factory")
    clients.get_fst_backfill_persistor_client = lambda: SimpleNamespace()
    printers = ModuleType("backfill_fst.backfill_fst_utils.printers")
    printers.print_flow_report = reports.append
    monkeypatch.setitem(sys.modules, clients.__name__, clients)
    monkeypatch.setitem(sys.modules, printers.__name__, printers)
    join = _flow_function("join_all_results")
    join.__globals__["_caught_exception_text"] = _flow_function("_caught_exception_text")
    flow = SimpleNamespace(storage_account_manifest_path="", end=object(), next=lambda step: None)
    succeeded = SimpleNamespace(machine_ids=["m1"], machine_and_af_output=[("m1", 4)])
    empty = SimpleNamespace(machine_ids=["m1"], machine_and_af_output=[])

    join(flow, [succeeded, empty])

    assert reports[0]["num_of_successful_machines"] == 1
    assert reports[0]["num_of_unsuccessful_machines"] == 0

    failed = SimpleNamespace(machine_ids=["m1"], fetch_samples_exception=RuntimeError("fetch failed"))
    with pytest.raises(RuntimeError, match="fetch failed"):
        join(flow, [succeeded, failed])
    assert reports[1]["num_of_successful_machines"] == 0
    assert reports[1]["unsuccessful_machines"] == ["m1"]


def test_ulrpm_keeps_monthly_branch_for_af_history():
    source = FLOW.read_text()
    assert "split_ulrpm_manifest" not in source
    assert "ulrpm_chunk_days" not in source
    assert "self.next(self.fetch_machine_samples, foreach='machines_manifest')" in source


def test_runner_serializes_ulrpm_but_keeps_standard_worker_setting(monkeypatch):
    definition = copy.deepcopy(next(node for node in ast.parse(BACKFILL_TRIGGER.read_text()).body
                                    if isinstance(node, ast.FunctionDef) and node.name == "execute_flow"))
    calls = []
    scope = {
        "os": os,
        "trigger_creation_of_flow": lambda command, max_workers=None: calls.append(max_workers) or True,
        "print_input_parameters": lambda *args: None,
        "colored": lambda message, *args, **kwargs: message,
    }
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(BACKFILL_TRIGGER), "exec"), scope)
    execute = scope["execute_flow"]
    kwargs = dict(base_dir="/example", namespace="dev", features_to_backfill="",
                  storage_account_manifest_path="", create_flow=True, trigger_flow=False)

    monkeypatch.setenv("FST_BACKFILL_RESOURCE_PROFILE", "ulrpm")
    execute(**kwargs)
    monkeypatch.setenv("FST_BACKFILL_RESOURCE_PROFILE", "standard")
    execute(**kwargs)

    assert calls == [1, None]

def test_orchestrator_exposes_machine_manifest_plan_and_v5_contract():
    source = ORCHESTRATOR.read_text()
    assert 'machine_manifest_plan' in source
    assert 'def parse_machine_manifest_plan' in source
    assert 'ORCHESTRATOR_CONTRACT_VERSION = "2026-10-06-v5"' in source
    from backfill_dashboard.admin import ULRPM_ORCHESTRATOR_CONTRACT_VERSION
    assert ULRPM_ORCHESTRATOR_CONTRACT_VERSION == "2026-10-06-v5"

    import ast
    from types import ModuleType
    module = ModuleType("orch_plan")
    tree = ast.parse(source)
    # Execute only the parse_machine_manifest_plan function and its deps by compiling needed names is heavy;
    # instead smoke-parse via exec of selected functions.
    # Extract and exec parse helpers + MONTHS constants via a trimmed namespace.
    ns: dict = {"List": list, "Dict": dict, "Optional": type(None), "json": __import__("json")}
    # Provide MONTHS long enough for indices 0..20
    ns["MONTHS"] = [(2024 + (i + 7) // 12, (i + 7) % 12 + 1) for i in range(30)]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {
            "parse_machine_manifest_plan",
        }:
            code = compile(ast.Module(body=[node], type_ignores=[]), ORCHESTRATOR.name, "exec")
            exec(code, ns)
    parse = ns["parse_machine_manifest_plan"]
    machine = "6817571193e37ef05fffcd1c"
    plan = {
        machine: [
            {"month_index": 19, "manifest": "p/month_19/part.parquet", "since": "2026/03/01/00", "until": "2026/03/08/00"},
            {"month_index": 19, "manifest": "p/month_19/part2.parquet", "since": "2026/03/08/00", "until": "2026/03/15/00"},
        ]
    }
    import json
    parsed = parse(json.dumps(plan), [machine], {machine: [19]})
    assert parsed is not None and len(parsed[machine]) == 2
    assert parse("", [machine], {machine: [19]}) is None

