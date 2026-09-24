from datetime import date

import pytest

from backfill_dashboard.campaigns import idempotency_key, plan_machine_windows
from backfill_dashboard.capacity import NodeCapacity, recommend_concurrency
from backfill_dashboard.classification import classify_machine
from backfill_dashboard.estimation import allocated_cost, makespan_estimate
from backfill_dashboard.inventory_models import MachineRecord


def test_classification_fails_closed_and_mixed_hardware_is_ulrpm():
    assert classify_machine({"tags": [], "endpoints": [{"type": "apus_alpha"}]}).cohort == "standard"
    assert classify_machine({"tags": [], "endpoints": [{"type": "apus_alpha"}, {"type": "low_rpm_us"}]}).cohort == "ulrpm"
    assert classify_machine({"tags": [], "endpoints": [{"type": "future_hw"}]}).cohort == "unknown"
    assert classify_machine({"tags": ["ulrpm"]}).cohort == "ulrpm"


def test_capacity_respects_node_fragmentation_and_zero_recommendation():
    result = recommend_concurrency([NodeCapacity("n1", 7000, 4000, 10)], memory_mib=8192, cpu_millicores=1000, argo_slots=20, configured_ceiling=20, fst_ceiling=20, dispatcher_ceiling=20)
    assert result.raw_slots == 0
    assert result.recommended_concurrency == 0


def test_campaign_windows_are_sequential_and_idempotent():
    machine = MachineRecord(machine_id="m1", resource_cohort="standard", backfill_eligible=True, classification_source_version="v1")
    items = plan_machine_windows(machine, campaign_id="c", start_at=date(2025, 1, 1), end_at=date(2025, 1, 11), window_days=5, feature_set_version="f", config_digest="d")
    assert [item.state for item in items] == ["ready", "blocked"]
    assert items[0].idempotency_key == idempotency_key("c", "m1", date(2025, 1, 1), date(2025, 1, 6), "f", "d")
    with pytest.raises(ValueError):
        plan_machine_windows(MachineRecord(machine_id="u"), campaign_id="c", start_at=date(2025, 1, 1), end_at=date(2025, 1, 2), window_days=1, feature_set_version="f", config_digest="d")


def test_estimate_uses_machine_critical_path_and_allocation_share():
    estimate = makespan_estimate({"a": [10, 10], "b": [3]}, 2)
    assert estimate.likely == 20
    assert allocated_cost(2, 2, 4, 4, 8, 1) == 1
