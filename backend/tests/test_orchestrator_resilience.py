from __future__ import annotations

import importlib.util
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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


def test_machine_lane_records_child_failure_and_stops(monkeypatch, tmp_path):
    """Fail-fast: a failed child blocks remaining chunks; no further triggers."""
    flow = _load_flow_module()
    triggered = []

    def trigger(*args):
        run_id = f"argo-fstbackfill-{len(triggered)}"
        triggered.append(run_id)
        return run_id

    monkeypatch.setattr(flow, "trigger_child_run", trigger)
    monkeypatch.setattr(flow, "wait_for_child_run", lambda *args, **kwargs: "failed")
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

    assert result["status"] == "stopped_on_failure"
    assert result["planned_months"] == 2
    assert result["completed_months"] == 0
    assert result["blocked_months"] == 1
    assert [item["month_index"] for item in result["failed_months"]] == [0]
    assert triggered == ["argo-fstbackfill-0"]
    events = flow.TraceLedger(tmp_path / "trace.jsonl").read_all()
    assert any(event["month_index"] == 0 and event["status"] == "failed" for event in events)
    assert any(
        event["month_index"] == 1
        and event["status"] == "blocked_by_previous_failure"
        and event.get("next_action") == "stop_lane_and_retry_after_diagnosis"
        for event in events
    )
    assert not any(event["month_index"] == 1 and event["status"] == "succeeded" for event in events)


def test_no_fe_output_is_terminal_success(monkeypatch, tmp_path):
    flow = _load_flow_module()
    monkeypatch.setattr(flow, "trigger_child_run", lambda *args: "argo-fstbackfill-empty")
    monkeypatch.setattr(flow, "wait_for_child_run", lambda *args, **kwargs: "succeeded")
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

    assert flow.wait_for_child_run("argo-child-123", poll_seconds=0, max_wait_seconds=10) == "failed"
    assert calls == [("FSTBackfill", "child-123")]


def test_wait_for_child_run_uses_argo_succeeded_for_unfinished_metaflow(monkeypatch):
    flow = _load_flow_module()
    calls = _install_status_stubs(monkeypatch, flow, "Succeeded")

    assert flow.wait_for_child_run("argo-child-456", poll_seconds=0, max_wait_seconds=10) == "succeeded"
    assert calls == [("FSTBackfill", "child-456")]


def test_wait_for_child_run_times_out_without_sleeping(monkeypatch):
    flow = _load_flow_module()
    _install_status_stubs(monkeypatch, flow, None)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(flow.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(flow.time, "sleep", lambda _: None)

    assert flow.wait_for_child_run("argo-child-timeout", poll_seconds=30, max_wait_seconds=1) == "timeout"


def _sep_2025_week_plan(machine_id: str) -> list[dict]:
    """Five chronological week chunks for month_index 13 (Sep 2025)."""
    windows = [
        ("2025/09/01/00", "2025/09/08/00"),
        ("2025/09/08/00", "2025/09/15/00"),
        ("2025/09/15/00", "2025/09/22/00"),
        ("2025/09/22/00", "2025/09/29/00"),
        ("2025/09/29/00", "2025/10/01/00"),
    ]
    entries = []
    for i, (since, until) in enumerate(windows):
        entries.append({
            "month_index": 13,
            "manifest": f"m/{machine_id}/week_{i:02d}.parquet",
            "since": since,
            "until": until,
        })
    return entries


def test_machine_lane_week_chunks_strictly_sequential(monkeypatch, tmp_path):
    flow = _load_flow_module()
    machine_id = "6817571193e37ef05fffcd1c"
    entries = _sep_2025_week_plan(machine_id)
    in_flight = {"count": 0, "max": 0}
    lock = threading.Lock()
    order = []
    waits = []

    def trigger(name_space, manifest, max_parallel_steps):
        with lock:
            in_flight["count"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["count"])
            order.append(("trigger", manifest))
        return f"argo-{Path(manifest).stem}"

    def wait(run_id, *args, **kwargs):
        waits.append(run_id)
        order.append(("wait", run_id))
        with lock:
            in_flight["count"] -= 1
        return "succeeded"

    monkeypatch.setattr(flow, "trigger_child_run", trigger)
    monkeypatch.setattr(flow, "wait_for_child_run", wait)
    monkeypatch.setattr(flow, "validate_start_logs", lambda *a: "start/task")
    monkeypatch.setattr(flow, "validate_af_logs", lambda *a: ("af/task", False))
    monkeypatch.setattr(flow, "validate_report_logs", lambda *a: "report/task")

    result = flow.run_machine_lane(
        batch_id="week-seq",
        machine_id=machine_id,
        indices=[13],
        name_space="feature-store-container",
        manifest_template="unused.parquet",
        max_parallel_steps=1,
        ledger_path=tmp_path / "trace.jsonl",
        manifest_entries=entries,
    )

    assert result["status"] == "completed"
    assert result["completed_months"] == 5
    assert in_flight["max"] == 1
    # trigger(n+1) only after wait(n) succeeded — interleaved order
    expected_manifests = [e["manifest"] for e in entries]
    trigger_manifests = [m for kind, m in order if kind == "trigger"]
    assert trigger_manifests == expected_manifests
    # Chronological since stamps: 09-01, 09-08, 09-15, 09-22, 09-29
    assert [e["since"][5:10] for e in entries] == ["09/01", "09/08", "09/15", "09/22", "09/29"]
    for i in range(len(waits)):
        # After wait i, the next trigger (if any) must be later in order
        wait_pos = order.index(("wait", waits[i]))
        if i + 1 < len(trigger_manifests):
            next_trig_pos = order.index(("trigger", trigger_manifests[i + 1]))
            assert wait_pos < next_trig_pos


def test_machine_lane_timeout_blocks_remaining_without_further_triggers(monkeypatch, tmp_path):
    flow = _load_flow_module()
    machine_id = "6817571193e37ef05fffcd1c"
    entries = _sep_2025_week_plan(machine_id)
    triggered = []

    def trigger(*args):
        triggered.append(args[1])
        return f"argo-{len(triggered)}"

    monkeypatch.setattr(flow, "trigger_child_run", trigger)
    monkeypatch.setattr(flow, "wait_for_child_run", lambda *a, **k: "timeout")
    monkeypatch.setattr(flow, "validate_start_logs", lambda *a: "start/task")
    monkeypatch.setattr(flow, "validate_af_logs", lambda *a: ("af/task", False))
    monkeypatch.setattr(flow, "validate_report_logs", lambda *a: "report/task")

    result = flow.run_machine_lane(
        batch_id="timeout-test",
        machine_id=machine_id,
        indices=[13],
        name_space="ns",
        manifest_template="unused.parquet",
        max_parallel_steps=1,
        ledger_path=tmp_path / "trace.jsonl",
        manifest_entries=entries,
    )

    assert result["status"] == "stopped_on_failure"
    assert len(triggered) == 1
    assert result["blocked_months"] == 4
    events = flow.TraceLedger(tmp_path / "trace.jsonl").read_all()
    assert any(e["status"] == "timeout" for e in events)
    assert sum(1 for e in events if e["status"] == "blocked_by_previous_failure") == 4


def test_machine_lane_exception_after_run_id_stops_lane(monkeypatch, tmp_path):
    flow = _load_flow_module()
    machine_id = "6817571193e37ef05fffcd1c"
    entries = _sep_2025_week_plan(machine_id)[:3]
    triggered = []

    monkeypatch.setattr(
        flow, "trigger_child_run",
        lambda *a: triggered.append(a[1]) or f"argo-{len(triggered)}",
    )
    monkeypatch.setattr(flow, "wait_for_child_run", lambda *a, **k: "succeeded")
    monkeypatch.setattr(flow, "validate_start_logs", lambda *a: (_ for _ in ()).throw(RuntimeError("log boom")))
    monkeypatch.setattr(flow, "validate_af_logs", lambda *a: ("af/task", False))
    monkeypatch.setattr(flow, "validate_report_logs", lambda *a: "report/task")

    result = flow.run_machine_lane(
        batch_id="exc-test",
        machine_id=machine_id,
        indices=[13],
        name_space="ns",
        manifest_template="unused.parquet",
        max_parallel_steps=1,
        ledger_path=tmp_path / "trace.jsonl",
        manifest_entries=entries,
    )

    assert result["status"] == "stopped_on_failure"
    assert len(triggered) == 1
    assert result["blocked_months"] == 2
    events = flow.TraceLedger(tmp_path / "trace.jsonl").read_all()
    assert any(e["status"] == "validation_failed" and e.get("child_run_id") for e in events)


def test_lane_internal_error_path_uses_terminal_by_key(monkeypatch, tmp_path):
    """Regression: lane-internal-error handler must not NameError on terminal_by_key."""
    flow = _load_flow_module()
    machine_id = "6817571193e37ef05fffcd1c"
    ledger_path = tmp_path / "trace.jsonl"

    # Seed one terminal event so the handler has something to skip.
    flow.TraceLedger(ledger_path).append(flow.TraceEvent(
        batch_id="internal",
        machine_id=machine_id,
        month_index=0,
        year=2024,
        month=8,
        manifest_path="month_00_2024_08.parquet",
        status="succeeded",
        event_time="2026-09-16T00:00:00+00:00",
    ))

    # Recreate the handler body that previously referenced terminal_by_index.
    terminal_statuses = {
        "succeeded", "skipped_no_fe_output", "failed", "timeout",
        "validation_failed", "cancelled", "lane_internal_error",
        "blocked_by_previous_failure",
    }
    manifest_plan = None
    terminal_by_key = {}
    for event in flow.TraceLedger(ledger_path).read_all():
        if (
            event.get("machine_id") == machine_id
            and event.get("status") in terminal_statuses
            and isinstance(event.get("month_index"), int)
        ):
            key = event.get("manifest_path") if manifest_plan else event["month_index"]
            if key is not None:
                terminal_by_key[key] = event
    assert 0 in terminal_by_key
    # Ensure the orchestrator source no longer contains the old name.
    source = FLOW_PATH.read_text()
    assert "terminal_by_index" not in source
    assert "terminal_by_key" in source


def test_two_machines_keep_own_order_without_self_overlap(monkeypatch, tmp_path):
    flow = _load_flow_module()
    m1, m2 = "machine-aaa", "machine-bbb"
    plans = {m1: _sep_2025_week_plan(m1), m2: _sep_2025_week_plan(m2)}
    locks = {m1: threading.Lock(), m2: threading.Lock()}
    in_flight = {m1: 0, m2: 0}
    max_flight = {m1: 0, m2: 0}
    order = {m1: [], m2: []}
    global_lock = threading.Lock()

    def trigger(name_space, manifest, max_parallel_steps):
        machine_id = m1 if m1 in manifest else m2
        with locks[machine_id]:
            in_flight[machine_id] += 1
            max_flight[machine_id] = max(max_flight[machine_id], in_flight[machine_id])
            assert in_flight[machine_id] == 1, f"{machine_id} overlapped itself"
        with global_lock:
            order[machine_id].append(manifest)
        return f"argo-{machine_id}-{Path(manifest).stem}"

    def wait(run_id, *args, **kwargs):
        mid = m1 if m1 in run_id else m2
        with locks[mid]:
            in_flight[mid] -= 1
        return "succeeded"

    monkeypatch.setattr(flow, "trigger_child_run", trigger)
    monkeypatch.setattr(flow, "wait_for_child_run", wait)
    monkeypatch.setattr(flow, "validate_start_logs", lambda *a: "start/task")
    monkeypatch.setattr(flow, "validate_af_logs", lambda *a: ("af/task", False))
    monkeypatch.setattr(flow, "validate_report_logs", lambda *a: "report/task")

    def run_lane(mid):
        return flow.run_machine_lane(
            batch_id="multi",
            machine_id=mid,
            indices=[13],
            name_space="ns",
            manifest_template="unused.parquet",
            max_parallel_steps=1,
            ledger_path=tmp_path / f"trace-{mid}.jsonl",
            manifest_entries=plans[mid],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(run_lane, m1)
        f2 = pool.submit(run_lane, m2)
        r1, r2 = f1.result(), f2.result()

    assert r1["status"] == "completed" and r2["status"] == "completed"
    assert max_flight[m1] == 1 and max_flight[m2] == 1
    assert order[m1] == [e["manifest"] for e in plans[m1]]
    assert order[m2] == [e["manifest"] for e in plans[m2]]


def test_parse_machine_manifest_plan_rejects_reversed_overlapping_duplicates():
    flow = _load_flow_module()
    mid = "6817571193e37ef05fffcd1c"
    indices = {mid: [13]}

    good = {
        mid: [
            {"month_index": 13, "manifest": "a.parquet", "since": "2025/09/01/00", "until": "2025/09/08/00"},
            {"month_index": 13, "manifest": "b.parquet", "since": "2025/09/08/00", "until": "2025/09/15/00"},
        ]
    }
    assert flow.parse_machine_manifest_plan(json.dumps(good), [mid], indices)[mid][0]["since"] == "2025/09/01/00"

    reversed_plan = {
        mid: [
            {"month_index": 13, "manifest": "b.parquet", "since": "2025/09/08/00", "until": "2025/09/15/00"},
            {"month_index": 13, "manifest": "a.parquet", "since": "2025/09/01/00", "until": "2025/09/08/00"},
        ]
    }
    try:
        flow.parse_machine_manifest_plan(json.dumps(reversed_plan), [mid], indices)
        assert False, "expected ValueError for reversed entries"
    except ValueError as exc:
        assert "sorted and non-overlapping" in str(exc)

    overlapping = {
        mid: [
            {"month_index": 13, "manifest": "a.parquet", "since": "2025/09/01/00", "until": "2025/09/10/00"},
            {"month_index": 13, "manifest": "b.parquet", "since": "2025/09/08/00", "until": "2025/09/15/00"},
        ]
    }
    try:
        flow.parse_machine_manifest_plan(json.dumps(overlapping), [mid], indices)
        assert False, "expected ValueError for overlapping entries"
    except ValueError as exc:
        assert "sorted and non-overlapping" in str(exc)

    duplicate = {
        mid: [
            {"month_index": 13, "manifest": "a.parquet", "since": "2025/09/01/00", "until": "2025/09/08/00"},
            {"month_index": 13, "manifest": "a.parquet", "since": "2025/09/01/00", "until": "2025/09/08/00"},
        ]
    }
    try:
        flow.parse_machine_manifest_plan(json.dumps(duplicate), [mid], indices)
        assert False, "expected ValueError for duplicates"
    except ValueError as exc:
        assert "duplicate" in str(exc).lower()
