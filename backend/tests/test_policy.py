from backfill_dashboard.parquet_inspector import ParquetFeatureSummary
from backfill_dashboard.policy import BackfillDecisionPolicy


def test_missing_partition_requires_backfill():
    decision = BackfillDecisionPolicy(["f1", "f2"]).missing_partition()

    assert decision.status == "needs_backfill"
    assert decision.missing_features == ["f1", "f2"]


def test_month_before_observed_coverage_is_not_an_actionable_gap():
    from backfill_dashboard.models import MonthPartition

    decision = BackfillDecisionPolicy(["f1"]).missing_partition_after(
        MonthPartition(index=0, year=2024, month=12),
        coverage_start=(2025, 8),
    )

    assert decision.status == "no_source_data"


def test_month_before_endpoint_installation_is_not_an_actionable_gap():
    from backfill_dashboard.models import MonthPartition

    decision = BackfillDecisionPolicy(["f1"]).missing_partition_after(
        MonthPartition(index=0, year=2026, month=7),
        coverage_start=(2025, 5),
        installation_start=(2026, 8),
    )

    assert decision.status == "no_source_data"
    assert "installation" in decision.reason


def test_month_after_lifecycle_coverage_is_not_an_actionable_gap_but_cutoff_month_is():
    from backfill_dashboard.models import MonthPartition

    policy = BackfillDecisionPolicy(["f1"])
    assert policy.missing_partition_after(
        MonthPartition(index=10, year=2026, month=6), None, (2026, 6)
    ).status == "needs_backfill"
    decision = policy.missing_partition_after(
        MonthPartition(index=11, year=2026, month=7), None, (2026, 6)
    )
    assert decision.status == "no_source_data"
    assert "last recorded source data" in decision.reason


def test_missing_feature_requires_backfill():
    policy = BackfillDecisionPolicy(["f1", "f2"])
    decision = policy.classify(
        ParquetFeatureSummary(
            row_count=10,
            columns=["machine_id", "f1"],
            feature_non_null_counts={"f1": 10, "f2": 0},
        )
    )

    assert decision.status == "needs_backfill"
    assert decision.missing_features == ["f2"]


def test_all_null_feature_requires_backfill():
    policy = BackfillDecisionPolicy(["f1", "f2"])
    decision = policy.classify(
        ParquetFeatureSummary(
            row_count=10,
            columns=["machine_id", "f1", "f2"],
            feature_non_null_counts={"f1": 10, "f2": 0},
        )
    )

    assert decision.status == "needs_backfill"
    assert decision.zero_count_features == ["f2"]


def test_populated_features_are_backfilled():
    policy = BackfillDecisionPolicy(["f1", "f2"])
    decision = policy.classify(
        ParquetFeatureSummary(
            row_count=10,
            columns=["machine_id", "f1", "f2"],
            feature_non_null_counts={"f1": 10, "f2": 7},
        )
    )

    assert decision.status == "backfilled"


def test_partially_populated_feature_requires_backfill():
    policy = BackfillDecisionPolicy(["f1_v2"])
    decision = policy.classify(
        ParquetFeatureSummary(
            row_count=10,
            columns=["f1", "f1_v2"],
            feature_non_null_counts={"f1_v2": 3},
            feature_coverage_gaps={
                "f1_v2": {
                    "missing_rows": 7,
                    "first_missing_at": "2026-04-01T00:00:00",
                    "last_missing_at": "2026-04-26T22:00:00",
                }
            },
        )
    )

    assert decision.status == "needs_backfill"
    assert decision.partial_features == ["f1_v2"]
    assert "7 source rows" in decision.reason
