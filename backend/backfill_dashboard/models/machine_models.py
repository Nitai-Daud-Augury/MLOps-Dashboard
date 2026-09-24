from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .status_models import BackfillStatus, MonthStatus


@dataclass
class MachineStatus:
    machine_id: str; augury_url: str; status: BackfillStatus; reason: str
    recommended_action: str; months_complete: int; months_expected: int
    coverage_start_month: str | None; no_source_months: int; first_missing_month: str | None
    last_populated_month: str | None; production_rows: int; silver_rows: int | None
    installation_at: str | None; installation_month: str | None
    online_months: int; offline_months: int; pre_install_months: int
    months: list[MonthStatus]
    display_name: str = ""; is_test_machine: bool = False


@dataclass
class DashboardSummary:
    machine_count: int; fully_backfilled: int; needs_backfill: int; unknown: int
    missing_partitions: int; partitions_with_missing_features: int; partitions_with_scan_errors: int
    production_rows: int; silver_rows: int | None; coverage_start_month: str | None = None
    eligible_partitions: int = 0; completed_eligible_partitions: int = 0; no_source_partitions: int = 0


@dataclass
class DashboardSnapshot:
    scan_id: str; generated_at: str; source_account: str; source_container: str
    target_features: list[str]; summary: DashboardSummary; machines: list[MachineStatus]
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls, *, account: str, container: str, target_features: list[str]) -> "DashboardSnapshot":
        return cls("not-scanned", datetime.now(timezone.utc).isoformat(), account, container, target_features,
                   DashboardSummary(0, 0, 0, 0, 0, 0, 0, 0, None), [], ["No scan has completed yet."])


@dataclass
class ScanState:
    scan_id: str = field(default_factory=lambda: uuid4().hex)
    running: bool = False; started_at: str | None = None; finished_at: str | None = None
    last_progress_at: str | None = None
    last_heartbeat_at: str | None = None
    error: str | None = None; source_account: str | None = None; source_container: str | None = None
    phase: str | None = None; completed_partitions: int = 0; total_partitions: int = 0
    completed_machines: int = 0; total_machines: int = 0


def to_dict(value: Any) -> Any:
    return asdict(value)
