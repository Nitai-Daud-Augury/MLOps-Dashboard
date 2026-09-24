from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Callable

from .config import Settings
from .activity import machine_month_coverage
from .inventory import MachineInventoryProvider
from .inventory_provider import MachineInventoryProvider as RichMachineInventoryProvider
from .models import (
    DashboardSnapshot,
    DashboardSummary,
    MachineStatus,
    MonthPartition,
    MonthStatus,
)
from .months import orchestrator_month_index, orchestrator_months
from .parquet_inspector import inspect_feature_partition
from .policy import BackfillDecisionPolicy
from .schema_contract import load_fst_schema_contract
from .silver import DatabricksSilverProvider
from .storage import BlobNotFoundError, BlobStore


class ScanCancelled(Exception):
    """Raised when a reset or replacement scan invalidates this read-only scan."""


def _iso_month(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.year, parsed.month
    except (TypeError, ValueError):
        return None


@dataclass
class BackfillScanner:
    settings: Settings
    inventory: MachineInventoryProvider
    blob_store: BlobStore
    silver_provider: DatabricksSilverProvider
    lifecycle_inventory: RichMachineInventoryProvider | None = None
    lifecycle_status: str = "not_configured"

    def scan(
        self,
        scan_id: str,
        progress_callback: Callable[[int, int, str], None] | None = None,
        machine_progress_callback: Callable[[int, int], None] | None = None,
        machine_result_callback: Callable[[MachineStatus], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> DashboardSnapshot:
        is_cancelled = is_cancelled or (lambda: False)
        self._raise_if_cancelled(is_cancelled)
        machine_ids = self.inventory.list_machine_ids()
        coverage_ends: dict[str, tuple[int, int]] = {}
        installation_starts: dict[str, tuple[int, int]] = {}
        installation_dates: dict[str, str] = {}
        display_names: dict[str, str] = {}
        test_machines: set[str] = set()
        if self.lifecycle_inventory is not None:
            try:
                lifecycle_records = self.lifecycle_inventory.get_many(machine_ids)
                for record in lifecycle_records:
                    if record.display_name:
                        display_names[record.machine_id] = record.display_name
                    if record.is_test_machine:
                        test_machines.add(record.machine_id)
                    installation_start = _iso_month(record.installation_at)
                    if installation_start and record.installation_at:
                        installation_starts[record.machine_id] = installation_start
                        installation_dates[record.machine_id] = record.installation_at
                    if record.archived or record.status.lower() in {"deactivated", "archived", "inactive", "disabled"}:
                        coverage_end = _iso_month(record.last_recorded_at or record.source_updated_at)
                        if coverage_end:
                            coverage_ends[record.machine_id] = coverage_end
                lifecycle_warning = (
                    f"Lifecycle filtering applied ({len(installation_starts)} installation date(s), "
                    f"{len(coverage_ends)} cutoff(s), status {self.lifecycle_status}); "
                    "the curated scan cohort was unchanged."
                )
            except Exception:
                lifecycle_warning = "Lifecycle lookup failed; lifecycle filtering was unavailable and the scan continued fail-open."
        else:
            lifecycle_warning = "Lifecycle filtering unavailable; the scan continued without lifecycle cutoffs."
        if machine_progress_callback:
            machine_progress_callback(0, len(machine_ids))
        if progress_callback:
            progress_callback(0, len(machine_ids), "discovering Feature Store coverage")
        coverage_starts = self._discover_coverage_starts(
            machine_ids,
            progress_callback=progress_callback,
            is_cancelled=is_cancelled,
        )
        self._raise_if_cancelled(is_cancelled)
        calendar_starts: dict[str, tuple[int, int]] = {}
        for machine_id in machine_ids:
            candidates = [
                value
                for value in (
                    coverage_starts.get(machine_id),
                    installation_starts.get(machine_id),
                )
                if value is not None
            ]
            if candidates:
                calendar_starts[machine_id] = min(candidates)
        partitions = self._coverage_partitions(calendar_starts)
        silver_counts = self._load_silver_counts(machine_ids, partitions)
        schema_contract = load_fst_schema_contract(self.settings.canonical_schema_path)
        policy = BackfillDecisionPolicy(
            self.settings.target_features,
            no_data_before=(self.settings.no_data_before_year, self.settings.no_data_before_month),
        )

        month_statuses: list[MonthStatus] = []
        total_partitions = len(machine_ids) * len(partitions)
        if progress_callback:
            progress_callback(0, total_partitions, "inspecting Parquet partitions")
        completed_partitions = 0
        completed_by_machine = {machine_id: 0 for machine_id in machine_ids}
        completed_machines = 0
        with ThreadPoolExecutor(max_workers=self.settings.scan_workers) as executor:
            futures = {
                executor.submit(
                    self._scan_month,
                    machine_id,
                    partition,
                    silver_counts,
                    policy,
                    coverage_starts.get(machine_id),
                    coverage_ends.get(machine_id),
                    installation_starts.get(machine_id),
                    is_cancelled,
                ): (
                    machine_id,
                    partition,
                )
                for machine_id in machine_ids
                for partition in partitions
            }
            for future in as_completed(futures):
                self._raise_if_cancelled(is_cancelled)
                month_statuses.append(future.result())
                completed_partitions += 1
                machine_id, _ = futures[future]
                completed_by_machine[machine_id] += 1
                if completed_by_machine[machine_id] == len(partitions):
                    completed_machines += 1
                    machine_months = sorted(
                        (item for item in month_statuses if item.machine_id == machine_id),
                        key=lambda item: item.partition.index,
                    )
                    self._apply_canonical_schema(
                        machine_months,
                        schema_contract.columns,
                        schema_contract.schema_version,
                    )
                    if machine_result_callback:
                        machine_result_callback(self._roll_up_machine(
                            machine_id,
                            machine_months,
                            coverage_starts.get(machine_id),
                            installation_dates.get(machine_id),
                            display_names.get(machine_id),
                            machine_id in test_machines,
                        ))
                    if machine_progress_callback:
                        machine_progress_callback(completed_machines, len(machine_ids))
                if progress_callback:
                    progress_callback(completed_partitions, total_partitions, "inspecting Parquet partitions")

        by_machine = {machine_id: [] for machine_id in machine_ids}
        for month_status in month_statuses:
            by_machine[month_status.machine_id].append(month_status)

        machines = [
            self._roll_up_machine(
                machine_id,
                sorted(months, key=lambda item: item.partition.index),
                coverage_starts.get(machine_id),
                installation_dates.get(machine_id),
                display_names.get(machine_id),
                machine_id in test_machines,
            )
            for machine_id, months in by_machine.items()
        ]
        summary = self._summarize(machines)
        warnings = []
        warnings.append(lifecycle_warning)
        global_start = min(coverage_starts.values(), default=None)
        if global_start:
            warnings.append(
                "Coverage starts at the first observed Feature Store partition: "
                f"{global_start[0]}-{global_start[1]:02d}. Earlier months are excluded per machine."
            )
        warnings.append(
            "Full-schema completeness is checked against canonical contract "
            f"{schema_contract.schema_version} ({len(schema_contract.columns)} columns)."
        )
        if not self.settings.silver_enabled:
            warnings.append("Databricks silver consistency is disabled; enable DATABRICKS_SILVER_ENABLED=1.")

        return DashboardSnapshot(
            scan_id=scan_id,
            generated_at=datetime.now(timezone.utc).isoformat(),
            source_account=self.settings.fst_account,
            source_container=self.settings.fst_container,
            target_features=self.settings.target_features,
            summary=summary,
            machines=machines,
            warnings=warnings,
        )

    def _load_silver_counts(
        self,
        machine_ids: list[str],
        partitions: list[MonthPartition],
    ) -> dict[tuple[str, int, int], int]:
        try:
            return self.silver_provider.row_counts(machine_ids, partitions)
        except Exception:
            return {}

    def _scan_month(
        self,
        machine_id: str,
        partition: MonthPartition,
        silver_counts: dict[tuple[str, int, int], int],
        policy: BackfillDecisionPolicy,
        coverage_start: tuple[int, int] | None,
        coverage_end: tuple[int, int] | None,
        installation_start: tuple[int, int] | None,
        is_cancelled: Callable[[], bool],
    ) -> MonthStatus:
        self._raise_if_cancelled(is_cancelled)
        blob_path = f"machine_id={machine_id}/{partition.blob_path_suffix}"
        silver_rows = silver_counts.get((machine_id, partition.year, partition.month))

        partition_key = (partition.year, partition.month)
        if installation_start and partition_key < installation_start:
            decision = policy.missing_partition_after(
                partition, coverage_start, coverage_end, installation_start
            )
            return MonthStatus(
                machine_id=machine_id,
                partition=partition,
                status=decision.status,
                reason=decision.reason,
                recommended_action=decision.recommended_action,
                blob_path=blob_path,
                silver_row_count=silver_rows,
                activity_status="not_installed",
            )

        if coverage_start is None or partition_key < coverage_start:
            decision = policy.missing_partition_after(
                partition, coverage_start, coverage_end, installation_start
            )
            return MonthStatus(
                machine_id=machine_id,
                partition=partition,
                status=decision.status,
                reason=decision.reason,
                recommended_action=decision.recommended_action,
                blob_path=blob_path,
                silver_row_count=silver_rows,
                missing_features=decision.missing_features,
                activity_status="offline",
            )

        try:
            blob = self.blob_store.read_parquet_metadata(blob_path)
            self._raise_if_cancelled(is_cancelled)
        except ScanCancelled:
            raise
        except BlobNotFoundError:
            decision = policy.missing_partition_after(
                partition, coverage_start, coverage_end, installation_start
            )
            return MonthStatus(
                machine_id=machine_id,
                partition=partition,
                status=decision.status,
                reason=decision.reason,
                recommended_action=decision.recommended_action,
                blob_path=blob_path,
                silver_row_count=silver_rows,
                missing_features=decision.missing_features,
                activity_status="offline",
            )
        except Exception as exc:
            decision = policy.scan_error(str(exc))
            return MonthStatus(
                machine_id=machine_id,
                partition=partition,
                status=decision.status,
                reason=decision.reason,
                recommended_action=decision.recommended_action,
                blob_path=blob_path,
                silver_row_count=silver_rows,
                error=str(exc),
                activity_status="unknown",
            )

        try:
            parquet_summary = inspect_feature_partition(blob.content, self.settings.target_features)
            decision = policy.classify(parquet_summary)
            return MonthStatus(
                machine_id=machine_id,
                partition=partition,
                status=decision.status,
                reason=decision.reason,
                recommended_action=decision.recommended_action,
                blob_path=blob_path,
                blob_url=blob.url,
                row_count=parquet_summary.row_count,
                silver_row_count=silver_rows,
                last_modified=blob.last_modified,
                missing_features=decision.missing_features,
                zero_count_features=decision.zero_count_features,
                partial_features=decision.partial_features,
                feature_coverage_gaps=parquet_summary.feature_coverage_gaps,
                feature_non_null_counts=parquet_summary.feature_non_null_counts,
                total_columns=len(parquet_summary.columns),
                columns=parquet_summary.columns,
                activity_status="online" if parquet_summary.row_count > 0 else "offline",
            )
        except ScanCancelled:
            raise
        except Exception as exc:
            # A footer can be unusually large or lack column statistics. In that
            # uncommon case retain the previous full-read behavior for correctness.
            try:
                self._raise_if_cancelled(is_cancelled)
                blob = self.blob_store.read_blob(blob_path)
                self._raise_if_cancelled(is_cancelled)
                parquet_summary = inspect_feature_partition(blob.content, self.settings.target_features)
                decision = policy.classify(parquet_summary)
                return MonthStatus(
                    machine_id=machine_id,
                    partition=partition,
                    status=decision.status,
                    reason=decision.reason,
                    recommended_action=decision.recommended_action,
                    blob_path=blob_path,
                    blob_url=blob.url,
                    row_count=parquet_summary.row_count,
                    silver_row_count=silver_rows,
                    last_modified=blob.last_modified,
                    missing_features=decision.missing_features,
                    zero_count_features=decision.zero_count_features,
                    partial_features=decision.partial_features,
                    feature_coverage_gaps=parquet_summary.feature_coverage_gaps,
                    feature_non_null_counts=parquet_summary.feature_non_null_counts,
                    total_columns=len(parquet_summary.columns),
                    columns=parquet_summary.columns,
                    activity_status="online" if parquet_summary.row_count > 0 else "offline",
                )
            except ScanCancelled:
                raise
            except Exception as fallback_exc:
                exc = fallback_exc
            decision = policy.scan_error(str(exc))
            return MonthStatus(
                machine_id=machine_id,
                partition=partition,
                status=decision.status,
                reason=decision.reason,
                recommended_action=decision.recommended_action,
                blob_path=blob_path,
                blob_url=blob.url,
                silver_row_count=silver_rows,
                last_modified=blob.last_modified,
                error=str(exc),
                activity_status="unknown",
            )

    def _roll_up_machine(
        self,
        machine_id: str,
        months: list[MonthStatus],
        coverage_start: tuple[int, int] | None,
        installation_at: str | None = None,
        display_name: str | None = None,
        is_test_machine: bool = False,
    ) -> MachineStatus:
        coverage = machine_month_coverage(months)
        complete = coverage["complete"]
        needs = coverage["needs"]
        scan_errors = coverage["scan_errors"]
        eligible = coverage["online"]
        no_source = coverage["no_source"]
        production_rows = sum(month.row_count or 0 for month in months)
        silver_values = [month.silver_row_count for month in months if month.silver_row_count is not None]
        silver_rows = sum(silver_values) if silver_values else None
        online_months = sum(month.activity_status == "online" for month in months)
        offline_months = sum(month.activity_status == "offline" for month in months)
        pre_install_months = sum(month.activity_status == "not_installed" for month in months)

        return MachineStatus(
            machine_id=machine_id,
            display_name=display_name or machine_id,
            is_test_machine=is_test_machine,
            augury_url=self.settings.augury_machine_url_template.format(machine_id=machine_id),
            status=coverage["status"],  # type: ignore[arg-type]
            reason=coverage["reason"],
            recommended_action=coverage["recommended_action"],
            months_complete=coverage["months_complete"],
            months_expected=coverage["months_expected"],
            coverage_start_month=(
                f"{coverage_start[0]}-{coverage_start[1]:02d}" if coverage_start else None
            ),
            no_source_months=len(no_source),
            first_missing_month=coverage["first_missing_month"],
            last_populated_month=coverage["last_populated_month"],
            production_rows=production_rows,
            silver_rows=silver_rows,
            installation_at=installation_at,
            installation_month=(
                f"{installation_start[0]}-{installation_start[1]:02d}"
                if (installation_start := _iso_month(installation_at))
                else None
            ),
            online_months=online_months,
            offline_months=offline_months,
            pre_install_months=pre_install_months,
            months=months,
        )

    def _summarize(self, machines: list[MachineStatus]) -> DashboardSummary:
        months = [month for machine in machines for month in machine.months]
        coverages = [machine_month_coverage(machine.months) for machine in machines]
        silver_values = [machine.silver_rows for machine in machines if machine.silver_rows is not None]
        return DashboardSummary(
            machine_count=len(machines),
            fully_backfilled=sum(item["status"] == "backfilled" for item in coverages),
            needs_backfill=sum(item["status"] == "needs_backfill" for item in coverages),
            unknown=sum(item["status"] in {"unknown", "scan_error"} for item in coverages),
            missing_partitions=sum(item["missing_partitions"] for item in coverages),
            partitions_with_missing_features=sum(item["missing_features"] for item in coverages),
            partitions_with_scan_errors=sum(len(item["scan_errors"]) for item in coverages),
            production_rows=sum(machine.production_rows for machine in machines),
            silver_rows=sum(silver_values) if silver_values else None,
            coverage_start_month=min(
                (machine.coverage_start_month for machine in machines if machine.coverage_start_month),
                default=None,
            ),
            eligible_partitions=sum(item["months_expected"] for item in coverages),
            completed_eligible_partitions=sum(item["months_complete"] for item in coverages),
            no_source_partitions=sum(len(item["no_source"]) for item in coverages),
        )

    def _apply_canonical_schema(
        self,
        months: list[MonthStatus],
        canonical_columns: tuple[str, ...],
        schema_version: str,
    ) -> None:
        """Compare every populated partition with the versioned producer schema contract."""
        expected_columns = set(canonical_columns)
        for month in months:
            if month.row_count is None or month.row_count == 0 or not month.columns:
                continue
            missing = sorted(expected_columns.difference(month.columns))
            month.expected_total_columns = len(expected_columns)
            month.missing_schema_columns = missing
            if not missing:
                continue

            schema_reason = (
                f"{len(missing)} canonical schema column(s) are missing against "
                f"{schema_version} ({len(expected_columns)} expected)."
            )
            if month.status == "needs_backfill":
                month.reason = f"{month.reason.rstrip('.')}; {schema_reason}"
            else:
                month.reason = schema_reason
            month.status = "needs_backfill"
            month.recommended_action = (
                "Backfill this partition with the full feature set, then rerun the scan."
            )

        # Column names are scanner-internal input. Do not inflate persisted/API snapshots
        # by serializing ~1,000 names for every complete machine-month.
        for month in months:
            month.columns = []

    def _discover_coverage_starts(
        self,
        machine_ids: list[str],
        progress_callback: Callable[[int, int, str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, tuple[int, int]]:
        is_cancelled = is_cancelled or (lambda: False)
        starts: dict[str, tuple[int, int]] = {}
        completed_machines = 0
        with ThreadPoolExecutor(max_workers=self.settings.scan_workers) as executor:
            futures = {
                executor.submit(
                    self._list_machine_blob_names,
                    machine_id,
                    is_cancelled,
                ): machine_id
                for machine_id in machine_ids
            }
            for future in as_completed(futures):
                self._raise_if_cancelled(is_cancelled)
                machine_id = futures[future]
                try:
                    months = _months_from_blob_names(future.result())
                except Exception:
                    months = []
                if months:
                    starts[machine_id] = min(months)
                completed_machines += 1
                if progress_callback:
                    progress_callback(
                        completed_machines,
                        len(machine_ids),
                        "discovering Feature Store coverage",
                    )
        return starts

    def _list_machine_blob_names(
        self,
        machine_id: str,
        is_cancelled: Callable[[], bool],
    ) -> list[str]:
        self._raise_if_cancelled(is_cancelled)
        names = self.blob_store.list_blob_names(f"machine_id={machine_id}/")
        self._raise_if_cancelled(is_cancelled)
        return names

    @staticmethod
    def _raise_if_cancelled(is_cancelled: Callable[[], bool]) -> None:
        if is_cancelled():
            raise ScanCancelled("Scan cancelled.")

    def _coverage_partitions(
        self,
        coverage_starts: dict[str, tuple[int, int]],
    ) -> list[MonthPartition]:
        """Build scan partitions with the ULRPM orchestrator's stable index.

        ``MonthPartition.index`` is also sent by the dashboard to the backfill
        orchestrator.  It must therefore be the fixed index in
        the shared ULRPM calendar (2024-08 is 0), not an index relative to a
        machine's first observed FST month.  The latter caused a selected
        2026-01/2026-03 pair to be submitted as indexes 9/11, i.e. 2025-05
        and 2025-07 in the orchestrator.
        """
        def partition(year: int, month: int) -> MonthPartition:
            return MonthPartition(
                index=orchestrator_month_index(year, month),
                year=year,
                month=month,
            )

        start = min(coverage_starts.values(), default=None)
        now = datetime.now(timezone.utc)
        end_year, end_month = now.year, now.month
        available_months = orchestrator_months(now)
        if start is None or start > (end_year, end_month):
            return [partition(year, month) for year, month in available_months]

        values: list[MonthPartition] = []
        year, month = start
        while (year, month) <= (end_year, end_month):
            values.append(partition(year, month))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return values


_BLOB_MONTH = re.compile(r"quarter=(\d{4})Q[1-4]/month=(\d{1,2})/partition_version=last/part-0\.parquet$")


def _months_from_blob_names(blob_names: list[str]) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    for blob_name in blob_names:
        match = _BLOB_MONTH.search(blob_name)
        if match:
            months.append((int(match.group(1)), int(match.group(2))))
    return months
