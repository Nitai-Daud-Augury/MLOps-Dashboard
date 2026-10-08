from __future__ import annotations

import os

from .config import get_settings
from .admin import AdminActionRepository, RunningWorkflowProvider, WorkflowCommandBuilder, WorkflowReviewRepository
from .inventory import FileMachineInventoryProvider
from .inventory_provider import FileMachineInventoryAdapter
from .lifecycle import build_lifecycle_provider
from .manifests import BackfillManifestWriter
from .repository import ReportRepository
from .scanner import BackfillScanner
from .silver import DatabricksSilverProvider
from .storage import AzureBlobDiscovery, AzureBlobStore
from .control_store import build_control_store
from .control_plane import ControlPlane
from .runtime_mode import runtime_info
from .data_sources import describe_data_sources, format_data_sources_log


def log_startup_data_sources(settings, file_inventory, lifecycle_inventory, lifecycle_status) -> None:
    """One ``[data-source]`` startup line per source (greppable; no secrets)."""
    from . import data_source_log as ds_log
    from .data_sources import _inventory_source
    from .feature_store_crosscheck import bronze_table, crosscheck_enabled, feature_store_table
    from .lifecycle import resolve_lifecycle_source

    configured = resolve_lifecycle_source()
    raw = os.getenv("LIFECYCLE_SOURCE")
    resolved = ds_log.describe_lifecycle_provider(lifecycle_inventory)
    ds_log.info(
        f"startup lifecycle: configured={configured} (LIFECYCLE_SOURCE={'unset, default' if raw is None else raw!r}) "
        f"status={lifecycle_status} using={resolved}"
    )
    if lifecycle_inventory is None and lifecycle_status != "off":
        ds_log.warning(
            f"startup lifecycle: {configured} provider unavailable (status {lifecycle_status}) -> scans run without "
            "lifecycle enrichment until a reconnect succeeds (no Mongo fallback)"
        )
    try:
        count: object = len(file_inventory.list_machine_ids())
    except Exception as exc:  # noqa: BLE001
        count = f"unknown ({ds_log.error_text(exc)})"
    ds_log.info(f"startup inventory: {_inventory_source(settings.machine_ids_file)} machines={count}")
    ds_log.info(f"startup features: source=blob account={settings.fst_account} container={settings.fst_container}")
    if crosscheck_enabled():
        try:
            ds_log.info(
                f"startup crosscheck: on (feature_store) table={feature_store_table()} "
                f"bronze_staleness_table={bronze_table() or 'off'}"
            )
        except Exception as exc:  # noqa: BLE001
            ds_log.warning(f"startup crosscheck: on but misconfigured: {ds_log.error_text(exc)} -> scans will fail open")
    else:
        ds_log.info("startup crosscheck: off (FEATURES_CROSSCHECK not set to feature_store)")


def build_components():
    settings = get_settings()
    runtime = runtime_info()
    file_inventory = FileMachineInventoryAdapter(
        FileMachineInventoryProvider(settings.machine_ids_file),
        settings.test_machine_ids or (),
    )
    # This console is intentionally limited to the curated 42-machine ULRPM
    # cohort. Do not re-enable Mongo here: it expands campaign scope beyond
    # the daily-monitoring population.
    inventory = file_inventory
    inventory_status = "local_file"
    blob_store = AzureBlobStore(settings.fst_account, settings.fst_container, scan_workers=settings.scan_workers)
    silver_provider = DatabricksSilverProvider(
        table=settings.silver_table,
        profile=settings.databricks_profile,
        warehouse_id=settings.warehouse_id,
        enabled=settings.silver_enabled,
    )
    lifecycle_inventory, lifecycle_status = build_lifecycle_provider()
    # Membership is file-based (settings.machine_ids_file); LIFECYCLE_SOURCE is
    # the single switch for enrichment only. Log both once at startup.
    import logging

    logging.getLogger("uvicorn.error").info(
        format_data_sources_log(
            describe_data_sources(settings, lifecycle_inventory, lifecycle_status, file_inventory)
        )
    )
    log_startup_data_sources(settings, file_inventory, lifecycle_inventory, lifecycle_status)
    # Coverage scanning remains scoped to the curated local ULRPM list.
    # Lifecycle enrichment (Mongo or Databricks) only looks up those IDs.
    scanner = BackfillScanner(settings, file_inventory, blob_store, silver_provider, lifecycle_inventory, lifecycle_status)
    repository = ReportRepository(settings)
    admin_repository = AdminActionRepository(settings.state_path.with_name("ulrpm_backfill_admin_actions.json"))
    workflow_reviews = WorkflowReviewRepository(settings.state_path.with_name("ulrpm_backfill_workflow_reviews.json"))
    command_builder = WorkflowCommandBuilder()
    manifest_writer = BackfillManifestWriter()
    running_workflows = RunningWorkflowProvider()
    blob_discovery = AzureBlobDiscovery(settings.fst_account, settings.fst_container)
    control_store = build_control_store(
        state_path=settings.state_path.with_name("ulrpm_backfill_control.json")
    )
    control_plane = ControlPlane(
        settings,
        inventory,
        inventory_status,
        manifest_writer,
        workflow_mutations_enabled=runtime.workflow_mutations,
    )
    return settings, scanner, repository, admin_repository, workflow_reviews, command_builder, manifest_writer, running_workflows, blob_discovery, control_store, control_plane
