from __future__ import annotations

from backfill_dashboard.config import Settings
from backfill_dashboard.models import MonthPartition, MonthStatus
from backfill_dashboard.scanner import BackfillScanner


def _month(month: int, columns: list[str]) -> MonthStatus:
    return MonthStatus(
        machine_id="machine",
        partition=MonthPartition(index=month, year=2026, month=month),
        status="backfilled",
        reason="All target ultrasonic v2 features are present with non-null values.",
        recommended_action="No backfill action required.",
        blob_path=f"month={month}/part-0.parquet",
        activity_status="online",
        row_count=10,
        total_columns=len(columns),
        columns=columns,
    )


def test_full_schema_gaps_change_month_and_machine_status() -> None:
    scanner = BackfillScanner(
        settings=Settings(),
        inventory=None,
        blob_store=None,
        silver_provider=None,
    )
    months = [
        _month(1, ["id", "feature_a", "feature_b"]),
        _month(2, ["id", "feature_a", "feature_b"]),
        _month(3, ["id", "feature_a", "feature_b"]),
        _month(4, ["id", "feature_a"]),
    ]

    scanner._apply_canonical_schema(
        months,
        ("id", "feature_a", "feature_b"),
        "test-schema-v1",
    )
    machine = scanner._roll_up_machine("machine", months, (2026, 1))

    assert months[3].status == "needs_backfill"
    assert months[3].expected_total_columns == 3
    assert months[3].missing_schema_columns == ["feature_b"]
    assert "canonical schema" in months[3].reason
    assert machine.status == "needs_backfill"
    assert machine.months_complete == 3
    assert all(month.columns == [] for month in months)


def test_canonical_schema_does_not_depend_on_machine_reference_months() -> None:
    scanner = BackfillScanner(
        settings=Settings(),
        inventory=None,
        blob_store=None,
        silver_provider=None,
    )
    months = [
        _month(1, ["id", "feature_a"]),
        _month(2, ["id", "feature_a", "feature_b"]),
        _month(3, ["id", "feature_a", "feature_b"]),
        _month(4, ["id", "feature_a", "feature_b"]),
    ]

    scanner._apply_canonical_schema(
        months,
        ("id", "feature_a", "feature_b"),
        "test-schema-v1",
    )

    assert months[0].missing_schema_columns == ["feature_b"]
    assert months[0].status == "needs_backfill"
    assert months[3].missing_schema_columns == []
    assert months[3].status == "backfilled"


def test_canonical_schema_applies_when_machine_has_no_reference_months() -> None:
    scanner = BackfillScanner(
        settings=Settings(),
        inventory=None,
        blob_store=None,
        silver_provider=None,
    )
    months = [_month(4, ["id", "feature_a"])]

    scanner._apply_canonical_schema(
        months,
        ("id", "feature_a", "feature_b"),
        "test-schema-v1",
    )
    machine = scanner._roll_up_machine("machine", months, (2026, 4))

    assert months[0].status == "needs_backfill"
    assert months[0].missing_schema_columns == ["feature_b"]
    assert "test-schema-v1" in months[0].reason
    assert machine.status == "needs_backfill"
