import pytest
from fastapi import HTTPException
from backfill_dashboard.admin import AdminAction

import backfill_dashboard.app as app_module
from backfill_dashboard.schemas import DailyGapManifestRequestModel
from backfill_dashboard.manifests import DailyGapManifestWriteResult, orchestrator_month_index


@pytest.mark.parametrize("status,activity", [("needs_backfill", "offline"), ("backfilled", "online")])
def test_daily_gap_api_rejects_ineligible_selection_before_any_upload(monkeypatch, status, activity):
    machine_id = "683ec59079fecb5a5a240478"
    index = orchestrator_month_index(2026, 1)
    machine = {
        "machine_id": machine_id,
        "months": [{"partition": {"year": 2026, "month": 1}, "status": status, "activity_status": activity}],
    }
    monkeypatch.setattr(app_module.repository, "latest", lambda: {"snapshot": {"machines": [machine]}})
    monkeypatch.setattr(app_module.manifest_writer, "write_daily_gap_manifests", lambda **kwargs: pytest.fail("must reject before upload"))
    with pytest.raises(HTTPException) as error:
        app_module.create_daily_gap_manifests(DailyGapManifestRequestModel(machine_id=machine_id, month_indices=[index]))
    assert error.value.status_code == 400
    assert "stale or ineligible" in error.value.detail


def test_daily_gap_api_rejects_unknown_machine_before_upload(monkeypatch):
    monkeypatch.setattr(app_module.repository, "latest", lambda: {"snapshot": {"machines": []}})
    monkeypatch.setattr(app_module.manifest_writer, "write_daily_gap_manifests", lambda **kwargs: pytest.fail("must reject before upload"))
    with pytest.raises(HTTPException):
        app_module.create_daily_gap_manifests(DailyGapManifestRequestModel(machine_id="683ec59079fecb5a5a240478", month_indices=[0]))


def test_daily_gap_api_records_completed_manifest_action(monkeypatch):
    machine_id = "683ec59079fecb5a5a240478"
    index = orchestrator_month_index(2026, 1)
    machine = {"machine_id": machine_id, "months": [{"partition": {"year": 2026, "month": 1}, "status": "needs_backfill", "activity_status": "online"}]}
    monkeypatch.setattr(app_module.repository, "latest", lambda: {"snapshot": {"machines": [machine]}})
    manifest = DailyGapManifestWriteResult(
        account_name="test", container_name="test", manifest_prefix="daily/unique",
        machine_id=machine_id, month_count=1, day_count=1,
        manifests=[{"manifest_path": "daily/unique/machine/2026/01/20260101.parquet", "since": "2026/01/01/00", "until": "2026/01/02/00"}],
        blob_url="https://example.invalid/daily/unique",
    )
    monkeypatch.setattr(app_module.manifest_writer, "write_daily_gap_manifests", lambda **kwargs: manifest)
    recorded = {}
    def record_completed(**kwargs):
        recorded.update(kwargs)
        return AdminAction(id="audit-1", action="manifest", status="succeeded", command=kwargs["command"], cwd=str(kwargs["cwd"]), created_at="now")
    monkeypatch.setattr(app_module.admin_repository, "record_completed", record_completed)

    result = app_module.create_daily_gap_manifests(DailyGapManifestRequestModel(machine_id=machine_id, month_indices=[index]))

    assert result["action"]["id"] == "audit-1"
    assert recorded["command"] == ["upload-daily-gap-manifests", "daily/unique"]
    assert recorded["machine_ids"] == [machine_id]
