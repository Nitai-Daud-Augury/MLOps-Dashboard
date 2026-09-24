from __future__ import annotations

from collections.abc import Iterator

from ..inventory_models import MachineRecord, MachineSearchQuery


def iter_selection(inventory, selection: dict) -> Iterator[MachineRecord]:
    explicit = list(dict.fromkeys(selection.get("explicit_machine_ids") or []))
    if explicit:
        if len(explicit) > 500:
            raise ValueError("explicit selection is capped at 500 machines; use select-all matching")
        records = inventory.get_many(explicit)
        if len(records) != len(explicit):
            found = {record.machine_id for record in records}
            raise ValueError("unknown machine IDs: " + ",".join(value for value in explicit if value not in found))
        yield from records
        return
    if selection.get("inventory_version") != inventory.get_version():
        raise ValueError("inventory version changed; refresh the selection")
    filters = selection.get("filter") or {}
    excluded = set(selection.get("excluded_machine_ids") or [])
    cursor = None
    while True:
        page = inventory.search(MachineSearchQuery(
            cursor=cursor, limit=200, search=filters.get("search") or "", cohort=filters.get("cohort"),
            status=filters.get("status"), eligible=filters.get("eligible"), site_id=filters.get("site_id"),
            organization_id=filters.get("organization_id"), classification_issue=filters.get("classification_issue"),
            sort_by=filters.get("sort_by") or "machine_id", sort_dir=filters.get("sort_dir") or "asc"))
        yield from (record for record in page.machines if record.machine_id not in excluded)
        cursor = page.next_cursor
        if not cursor:
            return


def normalized_selection(selection: dict, inventory_version: str) -> dict:
    explicit = sorted(set(selection.get("explicit_machine_ids") or []))
    excluded = sorted(set(selection.get("excluded_machine_ids") or []))
    return {"inventory_version": inventory_version, "explicit_machine_ids": explicit,
            "filter": selection.get("filter") or {}, "excluded_machine_ids": excluded}
