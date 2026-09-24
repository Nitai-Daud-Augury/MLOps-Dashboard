from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

BackfillStatus = Literal["backfilled", "needs_backfill", "no_source_data", "not_backfillable", "unknown", "scan_error"]
ActivityStatus = Literal["not_installed", "online", "offline", "unknown"]


@dataclass(frozen=True)
class MonthPartition:
    index: int
    year: int
    month: int

    @property
    def quarter(self) -> str:
        return f"{self.year}Q{((self.month - 1) // 3) + 1}"

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"

    @property
    def blob_path_suffix(self) -> str:
        return f"quarter={self.quarter}/month={self.month}/partition_version=last/part-0.parquet"


@dataclass
class MonthStatus:
    machine_id: str
    partition: MonthPartition
    status: BackfillStatus
    reason: str
    recommended_action: str
    blob_path: str
    blob_url: str | None = None
    row_count: int | None = None
    silver_row_count: int | None = None
    last_modified: str | None = None
    missing_features: list[str] = field(default_factory=list)
    zero_count_features: list[str] = field(default_factory=list)
    partial_features: list[str] = field(default_factory=list)
    feature_coverage_gaps: dict[str, dict[str, int | str]] = field(default_factory=dict)
    feature_non_null_counts: dict[str, int] = field(default_factory=dict)
    total_columns: int | None = None
    expected_total_columns: int | None = None
    missing_schema_columns: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    error: str | None = None
    activity_status: ActivityStatus = "unknown"
