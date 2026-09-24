from __future__ import annotations

from .config import get_settings
from .admin import AdminActionRepository, RunningWorkflowProvider, WorkflowCommandBuilder, WorkflowReviewRepository
from .inventory import FileMachineInventoryProvider
from .inventory_provider import FileMachineInventoryAdapter
from .mongo_inventory import build_mongo_provider
from .manifests import BackfillManifestWriter
from .repository import ReportRepository
from .scanner import BackfillScanner
from .silver import DatabricksSilverProvider
from .storage import AzureBlobDiscovery, AzureBlobStore
from .control_store import build_control_store
from .control_plane import ControlPlane
from .runtime_mode import runtime_info


def build_components():
    settings = get_settings()
    runtime = runtime_info()
    file_inventory = FileMachineInventoryAdapter(FileMachineInventoryProvider(settings.machine_ids_file))
    # This console is intentionally limited to the curated 42-machine ULRPM
    # cohort. Do not re-enable Mongo here: it expands campaign scope beyond
    # the daily-monitoring population.
    inventory = file_inventory
    inventory_status = "local_file"
    blob_store = AzureBlobStore(settings.fst_account, settings.fst_container)
    silver_provider = DatabricksSilverProvider(
        table=settings.silver_table,
        profile=settings.databricks_profile,
        warehouse_id=settings.warehouse_id,
        enabled=settings.silver_enabled,
    )
    lifecycle_inventory, lifecycle_status = build_mongo_provider()
    # Coverage scanning remains scoped to the curated local ULRPM list. The
    # 30k Mongo inventory is consumed only by the bounded campaign control plane.
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
