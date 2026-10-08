"""Lifecycle inventory source selection for scan enrichment.

``LIFECYCLE_SOURCE`` selects the provider:

- ``databricks`` — bronze ``machines_raw`` via SQL warehouse (preferred when set)
- ``mongo`` — existing Mongo machine/endpoint join (default when unset)
- ``openapi`` — reserved; OAuth not ready (returns not_configured)
- ``off`` — disable lifecycle enrichment
"""

from __future__ import annotations

import os

from .inventory_provider import MachineInventoryProvider


def resolve_lifecycle_source() -> str:
    """The single lifecycle switch: ``LIFECYCLE_SOURCE`` (default ``mongo``)."""
    source = (os.getenv("LIFECYCLE_SOURCE") or "mongo").strip().lower()
    return "off" if source in {"off", "none", "disabled"} else source


def build_lifecycle_provider() -> tuple[MachineInventoryProvider | None, str]:
    source = resolve_lifecycle_source()
    if source == "off":
        return None, "off"
    if source == "openapi":
        return None, "not_configured"
    if source == "databricks":
        from .lifecycle_databricks import build_databricks_lifecycle_provider

        return build_databricks_lifecycle_provider()
    if source == "mongo":
        from .mongo_inventory import build_mongo_provider

        return build_mongo_provider()
    return None, "not_configured"
