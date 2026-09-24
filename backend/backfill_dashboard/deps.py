"""Application-scoped component graph.

Route modules import these singletons rather than constructing infrastructure
clients during import or request handling.
"""
from .factory import build_components

(
    settings,
    scanner,
    repository,
    admin_repository,
    workflow_reviews,
    command_builder,
    manifest_writer,
    running_workflows,
    blob_discovery,
    control_store,
    control_plane,
) = build_components()

backfill_actions: dict[str, dict] = {}
