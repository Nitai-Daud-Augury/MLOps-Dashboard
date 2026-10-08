from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time
from typing import Any, Callable

from . import data_source_log as ds_log
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
from .feature_store_crosscheck import (
    FeatureStoreCrosscheck,
    apply_crosscheck,
    crosscheck_enabled,
    crosscheck_timeout_seconds,
    crosscheck_warning,
    error_summary,
    feature_store_table,
    row_tolerance,
)
from .lifecycle import build_lifecycle_provider, resolve_lifecycle_source
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


def _inventory_label(settings: Settings) -> str:
    try:
        from .data_sources import _inventory_source

        return _inventory_source(settings.machine_ids_file)
    except Exception:  # noqa: BLE001
        return f"file:{getattr(settings, 'machine_ids_file', '?')}"


def _tag_lifecycle(machine: MachineStatus, source: str | None, enriched_ids: set[str]) -> MachineStatus:
    """Per-machine provenance: which lifecycle source ran and whether it returned this machine."""
    machine.lifecycle_source = source
    machine.lifecycle_enriched = bool(source) and machine.machine_id.lower() in enriched_ids
    return machine


@dataclass
class BackfillScanner:
    settings: Settings
    inventory: MachineInventoryProvider
    blob_store: BlobStore
    silver_provider: DatabricksSilverProvider
    lifecycle_inventory: RichMachineInventoryProvider | None = None
    lifecycle_status: str = "not_configured"
    # Optional injected FEATURES_CROSSCHECK runner (tests); built lazily when
    # FEATURES_CROSSCHECK=feature_store and left None otherwise.
    feature_crosscheck: Any = None

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
        # Membership is file-based: the scan cohort is exactly the configured
        # machine-ids file (data/unique_machine_ids.txt). LIFECYCLE_SOURCE only
        # enriches these IDs (names, test flag, installation / first-recorded /
        # cutoff dates); it never adds or removes machines.
        machine_ids = self.inventory.list_machine_ids()
        scan_started = time.monotonic()
        lifecycle_source = resolve_lifecycle_source()
        ds_log.info(
            f"scan start: scan_id={scan_id} lifecycle={lifecycle_source} (status {self.lifecycle_status}) "
            f"inventory={_inventory_label(self.settings)} ({len(machine_ids)} machines) "
            f"features=blob {self.settings.fst_account}/{self.settings.fst_container} "
            f"crosscheck={'feature_store' if crosscheck_enabled() else 'off'}"
        )
        coverage_ends: dict[str, tuple[int, int]] = {}
        installation_starts: dict[str, tuple[int, int]] = {}
        installation_dates: dict[str, str] = {}
        first_recorded_starts: dict[str, tuple[int, int]] = {}
        display_names: dict[str, str] = {}
        test_machines: set[str] = set(self.settings.test_machine_ids or ())
        # Report progress before lifecycle enrichment so a hung Databricks
        # lookup cannot leave the UI on an empty "loading" scan forever.
        if machine_progress_callback:
            machine_progress_callback(0, len(machine_ids))
        if progress_callback:
            progress_callback(0, len(machine_ids), "enriching lifecycle metadata")
        lifecycle_inventory = self.lifecycle_inventory
        lifecycle_status = self.lifecycle_status
        if lifecycle_inventory is None and lifecycle_status in {"unreachable", "unauthorized"}:
            # The API may have started before the lifecycle backend became
            # reachable. Reconnect for each new scan instead of retaining that
            # startup failure for the lifetime of the process.
            ds_log.info(f"lifecycle reconnect: startup status was {lifecycle_status}; rebuilding {lifecycle_source} provider")
            try:
                lifecycle_inventory, lifecycle_status = build_lifecycle_provider()
            except Exception as exc:
                ds_log.warning(f"lifecycle reconnect failed: {ds_log.error_text(exc)}")
                lifecycle_inventory, lifecycle_status = None, "unreachable"
            ds_log.info(f"lifecycle reconnect result: status={lifecycle_status} provider={ds_log.describe_lifecycle_provider(lifecycle_inventory)}")
        enriched_ids: set[str] = set()
        lifecycle_used: str | None = None
        if lifecycle_inventory is not None:
            provider_label = ds_log.describe_lifecycle_provider(lifecycle_inventory)
            fetch_started = time.monotonic()
            try:
                lifecycle_records = lifecycle_inventory.get_many(machine_ids)
                requested = {machine_id.lower() for machine_id in machine_ids}
                enriched_ids = {record.machine_id.lower() for record in lifecycle_records} & requested
                lifecycle_used = lifecycle_source
                attempts = getattr(lifecycle_inventory, "last_fetch_attempts", None)
                ds_log.info(
                    f"lifecycle fetch: source={lifecycle_source} provider={provider_label} "
                    f"requested={len(machine_ids)} returned={len(lifecycle_records)} enriched={len(enriched_ids)} "
                    f"duration={time.monotonic() - fetch_started:.1f}s"
                    + (f" sql_attempts=[{'; '.join(attempts)}]" if attempts else "")
                )
                missing = sorted(requested - enriched_ids)
                if missing:
                    ds_log.warning(
                        f"lifecycle fetch: {len(missing)} machine(s) not found in {lifecycle_source} "
                        f"(e.g. {', '.join(missing[:5])}) -> scanned without lifecycle metadata"
                    )
                for record in lifecycle_records:
                    if record.display_name:
                        display_names[record.machine_id] = record.display_name
                    if record.is_test_machine:
                        test_machines.add(record.machine_id.lower())
                    installation_start = _iso_month(record.installation_at)
                    if installation_start and record.installation_at:
                        installation_starts[record.machine_id] = installation_start
                        installation_dates[record.machine_id] = record.installation_at
                    first_recorded_start = _iso_month(getattr(record, "first_recorded_at", None))
                    if first_recorded_start:
                        first_recorded_starts[record.machine_id] = first_recorded_start
                    if record.archived or record.status.lower() in {"deactivated", "archived", "inactive", "disabled"}:
                        coverage_end = _iso_month(record.last_recorded_at or record.source_updated_at)
                        if coverage_end:
                            coverage_ends[record.machine_id] = coverage_end
                lifecycle_warning = (
                    f"Lifecycle filtering applied ({len(installation_starts)} installation date(s), "
                    f"{len(coverage_ends)} cutoff(s), status {lifecycle_status}, source {resolve_lifecycle_source()}); "
                    "the curated scan cohort was unchanged."
                )
            except Exception as exc:
                enriched_ids = set()
                lifecycle_used = None
                ds_log.warning(
                    f"lifecycle fetch FAILED: source={lifecycle_source} provider={provider_label} after "
                    f"{time.monotonic() - fetch_started:.1f}s: {ds_log.error_text(exc)} -> fell back to: no lifecycle "
                    "enrichment for this scan (fail-open; no Mongo fallback)"
                )
                lifecycle_warning = "Lifecycle lookup failed; lifecycle filtering was unavailable and the scan continued fail-open."
        else:
            if lifecycle_status == "off":
                ds_log.info("lifecycle disabled (LIFECYCLE_SOURCE=off): scanning without lifecycle enrichment")
            else:
                ds_log.warning(
                    f"lifecycle unavailable: source={lifecycle_source} status={lifecycle_status} -> fell back to: no "
                    "lifecycle enrichment for this scan (no Mongo fallback)"
                )
            lifecycle_warning = (
                f"Lifecycle filtering unavailable (status: {lifecycle_status}, source: {resolve_lifecycle_source()}); "
                "the scan continued without lifecycle cutoffs or lifecycle test-machine metadata."
            )
        if progress_callback:
            progress_callback(0, len(machine_ids), "discovering Feature Store coverage")
        # Full set of months that actually have a Feature Store partition, per
        # machine. Machines whose listing failed are absent (unknown).
        partition_months: dict[str, frozenset[tuple[int, int]]] = {}
        coverage_starts = self._discover_coverage_starts(
            machine_ids,
            progress_callback=progress_callback,
            is_cancelled=is_cancelled,
            months_out=partition_months,
        )
        self._raise_if_cancelled(is_cancelled)
        # One data-start rule for every lifecycle source:
        # - data_start = earliest evidence of real data (lifecycle firstRecorded
        #   month or first FST partition). Missing months before it are
        #   no_source_data, never needs_backfill.
        # - The installation boundary can never hide real data: it is clamped to
        #   data_start, so a later endpoint installation date (e.g. a sensor
        #   swap in Mongo) cannot exclude months that were already recorded.
        calendar_starts: dict[str, tuple[int, int]] = {}
        data_starts: dict[str, tuple[int, int]] = {}
        effective_installation_starts: dict[str, tuple[int, int]] = {}
        for machine_id in machine_ids:
            evidence = [
                value
                for value in (
                    coverage_starts.get(machine_id),
                    first_recorded_starts.get(machine_id),
                )
                if value is not None
            ]
            if evidence:
                data_starts[machine_id] = min(evidence)
            installation_start = installation_starts.get(machine_id)
            if installation_start is not None:
                effective_installation_starts[machine_id] = min([installation_start, *evidence])
            candidates = [*evidence]
            if installation_start is not None:
                candidates.append(installation_start)
            if candidates:
                calendar_starts[machine_id] = min(candidates)
        partitions = self._coverage_partitions(calendar_starts)
        # Opt-in feature_store cross-check runs alongside the Parquet scan on its
        # own thread. It is informational only and fail-open.
        crosscheck_pool, crosscheck_future, crosscheck_runner, crosscheck_error = self._start_crosscheck(
            machine_ids, partitions, lifecycle_inventory
        )
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
                    effective_installation_starts.get(machine_id),
                    is_cancelled,
                    data_start=data_starts.get(machine_id),
                    partition_months=partition_months.get(machine_id),
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
                        machine_result_callback(_tag_lifecycle(self._roll_up_machine(
                            machine_id,
                            machine_months,
                            coverage_starts.get(machine_id),
                            installation_dates.get(machine_id),
                            display_names.get(machine_id),
                            machine_id.lower() in test_machines,
                        ), lifecycle_used, enriched_ids))
                    if machine_progress_callback:
                        machine_progress_callback(completed_machines, len(machine_ids))
                if progress_callback:
                    progress_callback(completed_partitions, total_partitions, "inspecting Parquet partitions")

        by_machine = {machine_id: [] for machine_id in machine_ids}
        for month_status in month_statuses:
            by_machine[month_status.machine_id].append(month_status)

        machines = [
            _tag_lifecycle(self._roll_up_machine(
                machine_id,
                sorted(months, key=lambda item: item.partition.index),
                coverage_starts.get(machine_id),
                installation_dates.get(machine_id),
                display_names.get(machine_id),
                machine_id.lower() in test_machines,
            ), lifecycle_used, enriched_ids)
            for machine_id, months in by_machine.items()
        ]
        summary = self._summarize(machines)
        crosscheck_summary = self._finish_crosscheck(
            machines, crosscheck_pool, crosscheck_future, crosscheck_runner, crosscheck_error
        )
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
        if crosscheck_summary is not None:
            warnings.append(crosscheck_warning(crosscheck_summary))

        if lifecycle_used:
            lifecycle_text = f"{lifecycle_used} ({len(enriched_ids)}/{len(machine_ids)} enriched, status {lifecycle_status})"
        else:
            lifecycle_text = f"none (source {lifecycle_source}, status {lifecycle_status}, 0/{len(machine_ids)} enriched)"
        if crosscheck_summary is None:
            crosscheck_text = "off"
        elif crosscheck_summary.get("status") == "error":
            crosscheck_text = "feature_store error (fail-open)"
        else:
            staleness = (crosscheck_summary.get("staleness") or {}).get("status")
            crosscheck_text = (
                f"feature_store ok (real={crosscheck_summary.get('real_mismatch_months', 0)}, "
                f"stale={crosscheck_summary.get('stale_months', 0)}, clean={crosscheck_summary.get('clean_months', 0)}, "
                f"staleness={staleness})"
            )
        ds_log.info(
            f"scan done: scan_id={scan_id} lifecycle={lifecycle_text}, "
            f"features=blob {self.settings.fst_account}/{self.settings.fst_container} "
            f"({total_partitions} machine-months), crosscheck={crosscheck_text}, "
            f"duration={time.monotonic() - scan_started:.1f}s"
        )

        return DashboardSnapshot(
            scan_id=scan_id,
            generated_at=datetime.now(timezone.utc).isoformat(),
            source_account=self.settings.fst_account,
            source_container=self.settings.fst_container,
            target_features=self.settings.target_features,
            summary=summary,
            machines=machines,
            warnings=warnings,
            crosscheck=crosscheck_summary,
        )

    def _start_crosscheck(
        self,
        machine_ids: list[str],
        partitions: list[MonthPartition],
        lifecycle_inventory: Any,
    ) -> tuple[ThreadPoolExecutor | None, Any, Any, BaseException | None]:
        if not crosscheck_enabled():
            return None, None, None, None
        try:
            runner = self.feature_crosscheck or FeatureStoreCrosscheck(lifecycle_inventory=lifecycle_inventory)
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fs-crosscheck")
            future = pool.submit(runner.fetch, list(machine_ids), list(partitions), list(self.settings.target_features))
            return pool, future, runner, None
        except Exception as exc:  # noqa: BLE001 - fail-open
            return None, None, None, exc

    def _finish_crosscheck(
        self,
        machines: list[MachineStatus],
        pool: ThreadPoolExecutor | None,
        future: Any,
        runner: Any,
        start_error: BaseException | None,
    ) -> dict[str, Any] | None:
        if future is None and start_error is None:
            return None
        table = getattr(runner, "table", None)
        tolerance = getattr(runner, "tolerance", None)
        if table is None:
            try:
                table = feature_store_table()
            except Exception:  # noqa: BLE001
                table = None
        if tolerance is None:
            tolerance = row_tolerance()
        try:
            if start_error is not None:
                raise start_error
            # Hard ceiling on top of the SQL path's own cold/steady timeouts.
            fetched = future.result(timeout=crosscheck_timeout_seconds())
            return apply_crosscheck(
                machines, fetched, list(self.settings.target_features), table=table, tolerance=tolerance
            )
        except Exception as exc:  # noqa: BLE001 - fail-open, statuses untouched
            ds_log.warning(
                f"crosscheck failed (table={table}): {ds_log.error_text(exc)} -> fell back to: no crosscheck for "
                "this scan (fail-open; statuses unaffected)"
            )
            for machine in machines:
                machine.crosscheck = None
                for month in machine.months:
                    month.crosscheck = None
            return error_summary(exc, table=table, tolerance=tolerance)
        finally:
            if pool is not None:
                pool.shutdown(wait=False)

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
        data_start: tuple[int, int] | None = None,
        partition_months: frozenset[tuple[int, int]] | None = None,
    ) -> MonthStatus:
        self._raise_if_cancelled(is_cancelled)
        blob_path = f"machine_id={machine_id}/{partition.blob_path_suffix}"
        silver_rows = silver_counts.get((machine_id, partition.year, partition.month))

        partition_key = (partition.year, partition.month)
        # Missing months are judged against the earliest evidence of data
        # (first-recorded month or first FST partition) for every source.
        policy_start = data_start if data_start is not None else coverage_start
        # Never skip a month that has a partition: the pre-install shortcut only
        # applies when coverage discovery saw no partition for this month
        # (partition_months is None when discovery could not list the machine).
        has_partition = partition_months is not None and partition_key in partition_months
        if installation_start and partition_key < installation_start and not has_partition:
            decision = policy.missing_partition_after(
                partition, policy_start, coverage_end, installation_start
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

        if not has_partition and (coverage_start is None or partition_key < coverage_start):
            decision = policy.missing_partition_after(
                partition, policy_start, coverage_end, installation_start
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
                partition, policy_start, coverage_end, installation_start
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
        months_out: dict[str, frozenset[tuple[int, int]]] | None = None,
    ) -> dict[str, tuple[int, int]]:
        """Return each machine's first FST month.

        When ``months_out`` is given it is filled with the full set of months
        that have a partition per machine (machines whose listing failed are
        left out, i.e. unknown).
        """
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
                    if months_out is not None:
                        months_out[machine_id] = frozenset(months)
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
