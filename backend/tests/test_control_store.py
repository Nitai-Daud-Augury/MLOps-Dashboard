from __future__ import annotations

import pytest

from backfill_dashboard.control_store import JsonControlStore


MACHINE_ID = "683ec59079fecb5a5a240478"


def test_fixed_month_state_is_idempotent_and_preserves_calendar_mapping(tmp_path):
    store = JsonControlStore(tmp_path / "control.json")

    queued = store.reserve_month(MACHINE_ID, 19, "action:first", "operator")
    assert (queued["year"], queued["month"], queued["state"]) == (2026, 3, "queued")

    with pytest.raises(ValueError, match="already queued"):
        store.reserve_month(MACHINE_ID, 19, "action:duplicate", "operator")


def test_cancel_and_requeue_have_distinct_safe_transitions(tmp_path):
    store = JsonControlStore(tmp_path / "control.json")
    store.reserve_month(MACHINE_ID, 17, "parent-1", "operator")
    store.reserve_month(MACHINE_ID, 19, "parent-1", "operator")

    cancelled = store.request_cancel(MACHINE_ID, 17, "parent-1", "child-jan", "operator")
    replacement = store.mark_requeue_requested(MACHINE_ID, 19, "parent-1", "operator", action_id="action-1")

    assert cancelled["state"] == "cancel_requested"
    assert cancelled["child_workflow_id"] == "child-jan"
    assert replacement["state"] == "requeue_pending"
    assert replacement["action_id"] == "action-1"


def test_parent_workflow_id_replaces_ephemeral_dashboard_action_id(tmp_path):
    store = JsonControlStore(tmp_path / "control.json")
    store.reserve_month(MACHINE_ID, 21, "action:temporary-id", "dashboard")

    linked = store.link_parent_workflow(MACHINE_ID, 21, "ulrpm-parent-123")

    assert linked["parent_workflow_id"] == "ulrpm-parent-123"
    assert linked["events"][-1]["state"] == "parent_linked"
