import pytest

from backfill_dashboard.activity import effective_activity_status, is_month_backfillable


@pytest.mark.parametrize("activity", ["offline", "not_installed", "unknown"])
@pytest.mark.parametrize("status", ["needs_backfill", "backfilled"])
def test_explicit_non_online_activity_never_becomes_backfillable(activity, status):
    month = {"activity_status": activity, "row_count": 100, "status": status}

    assert effective_activity_status(month) == activity
    assert not is_month_backfillable(month)


def test_legacy_month_infers_online_only_from_positive_row_count():
    assert effective_activity_status({"row_count": 1}) == "online"
    assert not is_month_backfillable({"row_count": 1, "status": "scan_error"})
    assert not is_month_backfillable({"row_count": 0, "status": "needs_backfill"})
    assert not is_month_backfillable({"status": "backfilled"})


@pytest.mark.parametrize("status", ["needs_backfill", "backfilled"])
def test_online_month_with_backfillable_status_is_allowed(status):
    assert is_month_backfillable({"activity_status": "online", "status": status})
