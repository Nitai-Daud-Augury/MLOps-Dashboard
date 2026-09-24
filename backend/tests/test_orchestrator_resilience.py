import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from sibling_repos import requires_ulrpm_orchestrator

pytestmark = requires_ulrpm_orchestrator

FLOW_PATH = (
    Path(__file__).parents[3]
    / "Augury repos"
    / "MLOps research"
    / "ulrpm_dev_backfill"
    / "UlrpmDevBackfillOrchestratorFlow.py"
)


def _load_flow_module():
    previous_metaflow = sys.modules.get("metaflow")
    metaflow = ModuleType("metaflow")
    metaflow.FlowSpec = object
    metaflow.Parameter = lambda *args, **kwargs: None

    def decorator(*args, **kwargs):
        def apply(value):
            return value

        return apply

    metaflow.kubernetes = decorator
    metaflow.project = decorator
    metaflow.step = lambda value: value
    sys.modules["metaflow"] = metaflow

    spec = importlib.util.spec_from_file_location("ulrpm_orchestrator_under_test", FLOW_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_metaflow is None:
            sys.modules.pop("metaflow", None)
        else:
            sys.modules["metaflow"] = previous_metaflow
    return module


def test_month_calendar_extends_through_runtime_current_month():
    flow = _load_flow_module()
    current = datetime.now(timezone.utc)

    assert flow.MONTHS[-1] == (current.year, current.month)
    assert flow.MONTHS[25] == (2026, 9)
    assert flow.parse_machine_month_indices(
        '{"6817571193e37ef05fffcd1c":[25]}',
        ["6817571193e37ef05fffcd1c"],
        0,
        -1,
    ) == {"6817571193e37ef05fffcd1c": [25]}


def test_machine_lane_records_child_failure_and_continues(monkeypatch, tmp_path):
    flow = _load_flow_module()
    triggered = iter(["argo-fstbackfill-failed", "argo-fstbackfill-succeeded"])
    outcomes = iter([False, True])

    monkeypatch.setattr(flow, "trigger_child_run", lambda *args: next(triggered))
    monkeypatch.setattr(flow, "wait_for_child_run", lambda *args, **kwargs: next(outcomes))
    monkeypatch.setattr(flow, "validate_start_logs", lambda *args: "start/task")
    monkeypatch.setattr(flow, "validate_af_logs", lambda *args: ("af/task", False))
    monkeypatch.setattr(flow, "validate_report_logs", lambda *args: "report/task")

    result = flow.run_machine_lane(
        batch_id="resilience-test",
        machine_id="6817571193e37ef05fffcd1c",
        indices=[0, 1],
        name_space="feature-store-container",
        manifest_template="month_{month_index:02d}_{year}_{month:02d}.parquet",
        max_parallel_steps=1,
        ledger_path=tmp_path / "trace.jsonl",
    )

    assert result["status"] == "completed_with_errors"
    assert result["planned_months"] == 2
    assert result["completed_months"] == 1
    assert [item["month_index"] for item in result["failed_months"]] == [0]
    events = flow.TraceLedger(tmp_path / "trace.jsonl").read_all()
    assert any(event["month_index"] == 0 and event["status"] == "failed" for event in events)
    assert any(event["month_index"] == 1 and event["status"] == "succeeded" for event in events)


def test_no_fe_output_is_terminal_success(monkeypatch, tmp_path):
    flow = _load_flow_module()
    monkeypatch.setattr(flow, "trigger_child_run", lambda *args: "argo-fstbackfill-empty")
    monkeypatch.setattr(flow, "wait_for_child_run", lambda *args, **kwargs: True)
    monkeypatch.setattr(flow, "validate_start_logs", lambda *args: "start/task")
    monkeypatch.setattr(flow, "validate_af_logs", lambda *args: ("af/task", True))
    monkeypatch.setattr(flow, "validate_report_logs", lambda *args: "report/task")

    result = flow.run_machine_lane(
        batch_id="empty-test",
        machine_id="683ec59079fecb5a5a240478",
        indices=[0],
        name_space="feature-store-container",
        manifest_template="month_{month_index:02d}_{year}_{month:02d}.parquet",
        max_parallel_steps=1,
        ledger_path=tmp_path / "trace.jsonl",
    )

    assert result["status"] == "completed"
    assert result["completed_months"] == 1
    assert result["skipped_no_fe_output_months"] == 1
    assert result["failed_months"] == []


def test_trace_file_failure_is_best_effort(monkeypatch, tmp_path, capsys):
    flow = _load_flow_module()
    ledger = flow.TraceLedger(tmp_path / "trace.jsonl")

    def fail_open(*args, **kwargs):
        raise OSError("diagnostics volume unavailable")

    monkeypatch.setattr(Path, "open", fail_open)
    ledger.append(
        flow.TraceEvent(
            batch_id="trace-test",
            machine_id="6817571193e37ef05fffcd1c",
            month_index=0,
            year=2024,
            month=8,
            manifest_path="month_00_2024_08.parquet",
            status="succeeded",
            event_time="2026-09-16T00:00:00+00:00",
        )
    )

    output = capsys.readouterr().out
    assert "TRACE_LEDGER_WARNING OSError" in output
    assert "TRACE " in output


def _install_status_stubs(monkeypatch, flow, phase):
    metaflow = ModuleType("metaflow")

    class UnfinishedRun:
        finished = False
        successful = False

    metaflow.Run = lambda path: UnfinishedRun()
    metaflow.namespace = lambda value: None
    argo = ModuleType("metaflow.plugins.argo.argo_workflows")
    calls = []

    class ArgoWorkflows:
        @staticmethod
        def get_workflow_status(flow_name, workflow_name):
            calls.append((flow_name, workflow_name))
            return phase

    argo.ArgoWorkflows = ArgoWorkflows
    monkeypatch.setitem(sys.modules, "metaflow", metaflow)
    monkeypatch.setitem(sys.modules, "metaflow.plugins", ModuleType("metaflow.plugins"))
    monkeypatch.setitem(sys.modules, "metaflow.plugins.argo", ModuleType("metaflow.plugins.argo"))
    monkeypatch.setitem(sys.modules, argo.__name__, argo)
    return calls


def test_wait_for_child_run_uses_argo_failed_for_unfinished_metaflow(monkeypatch):
    flow = _load_flow_module()
    calls = _install_status_stubs(monkeypatch, flow, "Failed")
    monkeypatch.setattr(flow.time, "sleep", lambda _: None)

    assert flow.wait_for_child_run("argo-child-123", poll_seconds=0, max_wait_seconds=10) is False
    assert calls == [("FSTBackfill", "child-123")]


def test_wait_for_child_run_uses_argo_succeeded_for_unfinished_metaflow(monkeypatch):
    flow = _load_flow_module()
    calls = _install_status_stubs(monkeypatch, flow, "Succeeded")

    assert flow.wait_for_child_run("argo-child-456", poll_seconds=0, max_wait_seconds=10) is True
    assert calls == [("FSTBackfill", "child-456")]


def test_wait_for_child_run_times_out_without_sleeping(monkeypatch):
    flow = _load_flow_module()
    _install_status_stubs(monkeypatch, flow, None)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(flow.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(flow.time, "sleep", lambda _: None)

    assert flow.wait_for_child_run("argo-child-timeout", poll_seconds=30, max_wait_seconds=1) is False
