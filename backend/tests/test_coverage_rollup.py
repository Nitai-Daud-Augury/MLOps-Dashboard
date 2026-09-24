from __future__ import annotations

import json
from types import SimpleNamespace

from backfill_dashboard.config import Settings
from backfill_dashboard.models import MonthPartition, MonthStatus
from backfill_dashboard.repository import ReportRepository
from backfill_dashboard.scanner import BackfillScanner


MACHINE_ID = "6847d5ed7fbbf944adb1eb0a"


def _month(index: int, status: str, activity: str, *, row_count: int | None = None, **kwargs):
    year, month = 2025 + (index - 1) // 12, (index - 1) % 12 + 1
    return MonthStatus(
        machine_id=MACHINE_ID,
        partition=MonthPartition(index=index, year=year, month=month),
        status=status,
        reason="test",
        recommended_action="test",
        blob_path=f"month={month}/part-0.parquet",
        activity_status=activity,
        row_count=row_count,
        **kwargs,
    )


def _scanner() -> BackfillScanner:
    return BackfillScanner(Settings(), None, None, None)


def _rollup(months: list[MonthStatus]):
    return _scanner()._roll_up_machine(MACHINE_ID, months, (2025, 1))


def test_online_gap_and_offline_gaps_count_only_online_coverage():
    months = [_month(1, "needs_backfill", "online")]
    months.extend(_month(i, "needs_backfill", "offline") for i in range(2, 14))

    machine = _rollup(months)
    summary = _scanner()._summarize([machine])

    assert (machine.months_complete, machine.months_expected, machine.status) == (0, 1, "needs_backfill")
    assert machine.first_missing_month == "2025-01"
    assert summary.eligible_partitions == 1
    assert summary.completed_eligible_partitions == 0
    assert summary.needs_backfill == 1
    assert summary.missing_partitions == 1


def test_offline_gaps_do_not_reduce_completed_online_coverage():
    months = [_month(1, "backfilled", "online", row_count=5)]
    months.extend(_month(i, "needs_backfill", "offline") for i in range(2, 14))

    machine = _rollup(months)
    summary = _scanner()._summarize([machine])

    assert (machine.months_complete, machine.months_expected, machine.status) == (1, 1, "backfilled")
    assert machine.first_missing_month is None
    assert summary.eligible_partitions == summary.completed_eligible_partitions == 1
    assert summary.needs_backfill == summary.missing_partitions == 0


def test_all_offline_months_are_zero_coverage_and_non_actionable():
    machine = _rollup([_month(i, "needs_backfill", "offline") for i in range(1, 13)])
    summary = _scanner()._summarize([machine])

    assert (machine.months_complete, machine.months_expected) == (0, 0)
    assert machine.status == "no_source_data"
    assert machine.first_missing_month is None
    assert summary.eligible_partitions == summary.completed_eligible_partitions == 0
    assert summary.needs_backfill == summary.missing_partitions == 0


def test_legacy_and_current_persisted_snapshots_are_normalized_on_read(tmp_path):
    state_path = tmp_path / "dashboard_state.json"
    machine = {
        "machine_id": MACHINE_ID,
        "status": "needs_backfill",
        "months_complete": 0,
        "months_expected": 13,
        "first_missing_month": "2025-01",
        "months": [
            {"status": "needs_backfill", "activity_status": "online", "partition": {"year": 2025, "month": 1}},
            *[
                {"status": "needs_backfill", "activity_status": "offline", "partition": {"year": 2025 + (i - 1) // 12, "month": (i - 1) % 12 + 1}}
                for i in range(2, 14)
            ],
        ],
    }
    original = {
        "machines": [machine],
        "summary": {"machine_count": 1, "needs_backfill": 1, "eligible_partitions": 13, "completed_eligible_partitions": 0},
    }
    state_path.write_text(json.dumps(original), encoding="utf-8")
    repo = ReportRepository(Settings(state_path=state_path))

    snapshot = repo.latest()["snapshot"]

    assert snapshot["machines"][0]["months_expected"] == 1
    assert snapshot["machines"][0]["status"] == "needs_backfill"
    assert snapshot["summary"]["eligible_partitions"] == 1
    assert snapshot["summary"]["missing_partitions"] == 1
    assert json.loads(state_path.read_text(encoding="utf-8")) == original

    legacy_path = tmp_path / "legacy_state.json"
    legacy_path.write_text(json.dumps({
        "machines": [{
            "machine_id": MACHINE_ID,
            "months": [
                {"status": "backfilled", "row_count": 3, "partition": {"year": 2026, "month": 1}},
                {"status": "needs_backfill", "partition": {"year": 2026, "month": 2}},
            ],
        }],
        "summary": {"eligible_partitions": 2, "completed_eligible_partitions": 1},
    }), encoding="utf-8")
    legacy = ReportRepository(Settings(state_path=legacy_path)).latest()["snapshot"]
    assert legacy["machines"][0]["months_expected"] == 1
    assert legacy["machines"][0]["status"] == "backfilled"
    assert legacy["summary"]["eligible_partitions"] == 1
    assert legacy["summary"]["completed_eligible_partitions"] == 1


def test_manifest_csv_excludes_offline_needs_backfill(monkeypatch):
    import backfill_dashboard.app as app_module

    monkeypatch.setattr(app_module, "repository", SimpleNamespace(latest=lambda: {
        "snapshot": {"machines": [{
            "machine_id": MACHINE_ID,
            "months": [
                {"status": "needs_backfill", "activity_status": "online", "partition": {"year": 2026, "month": 1}},
                {"status": "needs_backfill", "activity_status": "offline", "partition": {"year": 2026, "month": 2}},
                {"status": "needs_backfill", "partition": {"year": 2026, "month": 3}},
            ],
        }]},
    }))

    response = app_module.export_manifest()

    assert response.body.decode().splitlines() == [
        "machine_id,since,until",
        f"{MACHINE_ID},2026/01/01/00,2026/02/01/00",
    ]
