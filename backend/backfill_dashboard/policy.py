from __future__ import annotations

from dataclasses import dataclass, field

from .models import BackfillStatus, MonthPartition
from .parquet_inspector import ParquetFeatureSummary


@dataclass(frozen=True)
class BackfillDecision:
    status: BackfillStatus
    reason: str
    recommended_action: str
    missing_features: list[str]
    zero_count_features: list[str]
    partial_features: list[str] = field(default_factory=list)


class BackfillDecisionPolicy:
    def __init__(
        self,
        target_features: list[str],
        no_data_before: tuple[int, int] | None = None,
    ) -> None:
        self.target_features = target_features
        self.no_data_before = no_data_before

    def missing_partition(self, partition: MonthPartition | None = None) -> BackfillDecision:
        return self.missing_partition_after(partition, coverage_start=None)

    def missing_partition_after(
        self,
        partition: MonthPartition | None,
        coverage_start: tuple[int, int] | None,
        coverage_end: tuple[int, int] | None = None,
        installation_start: tuple[int, int] | None = None,
    ) -> BackfillDecision:
        if partition and installation_start and (partition.year, partition.month) < installation_start:
            return BackfillDecision(
                status="no_source_data",
                reason="Month predates this machine's endpoint installation in Augury.",
                recommended_action="Not installed yet; exclude this month from backfill coverage.",
                missing_features=[],
                zero_count_features=[],
            )
        if partition and not installation_start and coverage_start and (partition.year, partition.month) < coverage_start:
            return BackfillDecision(
                status="no_source_data",
                reason="Month predates this machine's first observed Feature Store partition.",
                recommended_action="Excluded from coverage until source data availability is established.",
                missing_features=[],
                zero_count_features=[],
            )
        if partition and self.no_data_before:
            cutoff_year, cutoff_month = self.no_data_before
            if (partition.year, partition.month) < (cutoff_year, cutoff_month):
                return BackfillDecision(
                    status="no_source_data",
                    reason="Partition is missing in a month known to often have no ULRPM source data.",
                    recommended_action="No backfill action unless raw sample availability is confirmed.",
                    missing_features=[],
                    zero_count_features=[],
                )
        if partition and coverage_end and (partition.year, partition.month) > coverage_end:
            return BackfillDecision(
                status="no_source_data",
                reason="Month is after the machine's last recorded source data; the machine was deactivated or its source ended.",
                recommended_action="Do not backfill unless source data resumed or machine lifecycle information is corrected.",
                missing_features=[],
                zero_count_features=[],
            )
        return BackfillDecision(
            status="needs_backfill",
            reason="Production FST partition is missing.",
            recommended_action="Verify raw sessions exist, then generate a backfill manifest for this month.",
            missing_features=self.target_features,
            zero_count_features=[],
        )

    def scan_error(self, error: str) -> BackfillDecision:
        return BackfillDecision(
            status="scan_error",
            reason=f"Could not inspect production FST partition: {error}",
            recommended_action="Fix scanner credentials/connectivity, then rerun the dashboard scan.",
            missing_features=[],
            zero_count_features=[],
        )

    def classify(self, summary: ParquetFeatureSummary) -> BackfillDecision:
        if summary.row_count == 0:
            return BackfillDecision(
                status="no_source_data",
                reason="Production FST partition exists but contains no rows.",
                recommended_action="Do not backfill until raw sample availability is verified.",
                missing_features=[],
                zero_count_features=[],
            )

        column_set = set(summary.columns)
        missing = [feature for feature in self.target_features if feature not in column_set]
        zero_counts = [
            feature
            for feature in self.target_features
            if feature in column_set and summary.feature_non_null_counts.get(feature, 0) == 0
        ]
        partial = [
            feature
            for feature in self.target_features
            if feature not in missing
            and feature not in zero_counts
            and summary.feature_coverage_gaps.get(feature, {}).get("missing_rows", 0) > 0
        ]
        if missing or zero_counts or partial:
            reason_parts = []
            if missing:
                reason_parts.append(f"{len(missing)} target feature columns are missing")
            if zero_counts:
                reason_parts.append(f"{len(zero_counts)} target feature columns are all null")
            if partial:
                missing_rows = max(
                    int(summary.feature_coverage_gaps[feature]["missing_rows"])
                    for feature in partial
                )
                reason_parts.append(
                    f"{len(partial)} target feature columns are incomplete on up to "
                    f"{missing_rows} source rows"
                )
            return BackfillDecision(
                status="needs_backfill",
                reason="; ".join(reason_parts) + ".",
                recommended_action="Add this partition to the ULRPM v2 backfill manifest.",
                missing_features=missing,
                zero_count_features=zero_counts,
                partial_features=partial,
            )

        return BackfillDecision(
            status="backfilled",
            reason=(
                "All target ultrasonic v2 features are present on every corresponding "
                "v1 source row."
            ),
            recommended_action="No backfill action required.",
            missing_features=[],
            zero_count_features=[],
        )
