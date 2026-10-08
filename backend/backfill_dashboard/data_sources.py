"""Read-only description of the dashboard's active data sources.

The machine list (scan cohort) is always file-based: ``ULRPM_MACHINE_IDS_FILE``
(default ``data/unique_machine_ids.txt``). ``LIFECYCLE_SOURCE`` is the single
switch for lifecycle *enrichment* (databricks | mongo | openapi | off); it never
adds or removes machines. Nothing here returns credentials or connection strings.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .feature_store_crosscheck import describe_crosscheck_config
from .lifecycle import resolve_lifecycle_source

_DASHBOARD_ROOT = Path(__file__).resolve().parents[2]


def _inventory_source(path: Any) -> str:
    try:
        resolved = Path(path).resolve()
        try:
            return f"file:{resolved.relative_to(_DASHBOARD_ROOT)}"
        except ValueError:
            return f"file:{resolved}"
    except Exception:
        return f"file:{path}"


def _last_crosscheck_run(summary: Any) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    keys = (
        "status", "ran_at", "checked_months", "flagged_months", "real_mismatch_months", "flagged_machines",
        "stale_months", "stale_months_with_differences", "clean_months", "flags_by_type", "error",
    )
    run = {key: summary[key] for key in keys if key in summary}
    staleness = summary.get("staleness")
    if isinstance(staleness, dict):
        run["staleness_status"] = staleness.get("status")
        if staleness.get("error"):
            run["staleness_error"] = staleness.get("error")
    return run


def describe_data_sources(
    settings: Any,
    lifecycle_inventory: Any,
    lifecycle_status: str,
    inventory: Any = None,
    last_crosscheck: Any = None,
) -> dict[str, Any]:
    source = resolve_lifecycle_source()
    info: dict[str, Any] = {
        "lifecycle_source": source,
        "lifecycle_status": lifecycle_status,
        "inventory_source": _inventory_source(getattr(settings, "machine_ids_file", "data/unique_machine_ids.txt")),
        "machine_count": None,
    }
    if source == "databricks":
        table = getattr(lifecycle_inventory, "table", None)
        if not table:
            from .lifecycle_databricks import _resolve_machines_table

            table = _resolve_machines_table()
        info["lifecycle_table"] = table
        info["lifecycle_warehouse_id"] = getattr(lifecycle_inventory, "warehouse_id", None) or (os.getenv("DATABRICKS_WAREHOUSE_ID") or "").strip() or None
        from .silver import auth_description

        profile = getattr(lifecycle_inventory, "profile", None) or (os.getenv("DATABRICKS_PROFILE") or "").strip() or None
        info["databricks_auth"] = auth_description(profile)
        # Profile is ignored inside Databricks Apps (service principal auth).
        info["databricks_profile"] = profile if info["databricks_auth"].startswith("profile=") else None
    if inventory is not None:
        try:
            info["machine_count"] = len(inventory.list_machine_ids())
        except Exception:
            info["machine_count"] = None
    # FEATURES_CROSSCHECK (informational blob-vs-feature_store check; blob stays
    # the source of truth for statuses).
    crosscheck = describe_crosscheck_config()
    crosscheck["last_run"] = _last_crosscheck_run(last_crosscheck)
    info["features_crosscheck"] = crosscheck
    return info


def format_data_sources_log(info: dict[str, Any]) -> str:
    keys = ("lifecycle_source", "lifecycle_status", "lifecycle_table", "lifecycle_warehouse_id", "databricks_profile", "databricks_auth", "inventory_source", "machine_count")
    parts = [f"{key}={info[key]}" for key in keys if key in info]
    crosscheck = info.get("features_crosscheck")
    if isinstance(crosscheck, dict):
        parts.append(f"features_crosscheck={crosscheck.get('mode', 'off')}")
        if crosscheck.get("enabled"):
            parts.append(f"feature_store_table={crosscheck.get('table')}")
            parts.append(f"feature_store_bronze_table={crosscheck.get('bronze_table')}")
            parts.append(f"crosscheck_staleness_gate={'on' if crosscheck.get('bronze_table') else 'off'}")
            parts.append(f"crosscheck_row_tolerance={crosscheck.get('row_tolerance')}")
        if crosscheck.get("config_error"):
            parts.append(f"crosscheck_config_error={crosscheck['config_error']!r}")
    return "data_sources: " + " ".join(parts)
