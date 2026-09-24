from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict
from typing import Any, Protocol, Sequence

from .classification import CLASSIFIER_VERSION, classify_machine
from .inventory import FileMachineInventoryProvider
from .inventory_models import MachineFacets, MachinePage, MachineRecord, MachineSearchQuery


class MachineInventoryProvider(Protocol):
    def search(self, query: MachineSearchQuery) -> MachinePage: ...
    def get_many(self, machine_ids: Sequence[str]) -> list[MachineRecord]: ...
    def get_facets(self, query: MachineSearchQuery) -> MachineFacets: ...
    def get_version(self) -> str: ...


def _record(document: dict[str, Any], version: str) -> MachineRecord:
    machine_id = str(document.get("machine_id", document.get("_id", "")))
    tags = document.get("tags")
    endpoints = document.get("endpoints")
    normalized = {**document, "tags": tags, "endpoints": endpoints}
    classification = classify_machine(normalized)
    status = str(document.get("status", "unknown"))
    archived = bool(document.get("archived", False))
    eligible_statuses = {value.strip().lower() for value in os.getenv("BACKFILL_ELIGIBLE_STATUSES", "active,deactivated").split(",") if value.strip()}
    eligible = classification.cohort != "unknown" and not archived and status.lower() in eligible_statuses
    last_recorded = document.get("lastRecorded", document.get("last_recorded_at", document.get("lastRecordedAt")))
    if isinstance(last_recorded, dict):
        last_recorded = last_recorded.get("timestamp")
    installation_at = _installation_at(document, endpoints)
    display_name = str(document.get("display_name", document.get("name", machine_id)))
    return MachineRecord(
        machine_id=machine_id,
        display_name=display_name,
        is_test_machine=_is_test_machine(display_name, tags),
        site_id=_optional_str(document.get("site_id", document.get("siteId"))),
        site_name=document.get("site_name", document.get("siteName")),
        organization_id=_optional_str(document.get("organization_id", document.get("organizationId"))),
        organization_name=document.get("organization_name", document.get("organizationName")),
        status=status, archived=archived,
        tags=tuple(str(x) for x in (tags or ()) if x is not None),
        endpoint_count=len(endpoints) if isinstance(endpoints, list) else 0,
        endpoint_hardware_types=tuple(str(x.get("hardware_type", x.get("type", ""))) for x in (endpoints or ()) if isinstance(x, dict)),
        resource_cohort=classification.cohort,
        classification_reason=classification.reasons,
        classification_source_version=classification.classifier_version,
        backfill_eligible=eligible,
        exclusion_reason=None if eligible else ("archived" if archived else "unknown_classification" if classification.cohort == "unknown" else f"status:{status}"),
        source_updated_at=_optional_iso(document.get("updated_at", document.get("updatedAt"))),
        last_recorded_at=_optional_iso(last_recorded),
        installation_at=installation_at,
        inventory_version=version,
    )


def _is_test_machine(display_name: str, tags: Any) -> bool:
    """Identify test inventory from descriptive metadata, never from machine IDs."""
    import re

    metadata = [display_name]
    if isinstance(tags, str):
        metadata.append(tags)
    elif isinstance(tags, (list, tuple, set)):
        for tag in tags:
            if isinstance(tag, dict):
                metadata.extend(str(tag.get(key, "")) for key in ("name", "label", "value"))
            elif tag is not None:
                metadata.append(str(tag))
    return any(re.search(r"(?i)(?:^|[^a-z0-9])(?:test(?:ing)?|e2e|qa)(?:$|[^a-z0-9])", value) for value in metadata)


def _installation_at(document: dict[str, Any], endpoints: Any) -> str | None:
    """Return the earliest ULRPM endpoint installation timestamp for a machine.

    Machine configuration creation time is not an installation boundary. Endpoint
    ``installation_date`` is the field shown by the Augury lifecycle data. Prefer
    ULRPM endpoints so an older standard sensor does not make ULRPM coverage start
    before the ULRPM hardware was installed.
    """
    candidates: list[tuple[str, bool]] = []
    for endpoint in endpoints if isinstance(endpoints, list) else ():
        if not isinstance(endpoint, dict):
            continue
        value = endpoint.get("installation_date", endpoint.get("installationDate"))
        normalized = _optional_iso(value)
        if not normalized:
            continue
        endpoint_type = endpoint.get("hardware_type", endpoint.get("type"))
        is_ulrpm = classify_machine({
            "tags": [],
            "endpoints": [{"type": endpoint_type}],
        }).cohort == "ulrpm" if endpoint_type else False
        candidates.append((normalized, is_ulrpm))

    direct = _optional_iso(document.get("installation_date", document.get("installationDate")))
    if direct:
        candidates.append((direct, False))
    preferred = [value for value, is_ulrpm in candidates if is_ulrpm]
    values = preferred or [value for value, _ in candidates]
    return min(values) if values else None


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(value: str | None) -> str:
    if not value:
        return ""
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except Exception as exc:
        raise ValueError("invalid inventory cursor") from exc


class FileMachineInventoryAdapter:
    def __init__(self, provider: FileMachineInventoryProvider):
        self.provider = provider
        self._records: list[MachineRecord] | None = None

    def _all(self) -> list[MachineRecord]:
        if self._records is None:
            self._records = [_record({"_id": mid, "tags": ["ulrpm"], "endpoints": [{"type": "low_rpm_us"}], "status": "active"}, "file") for mid in self.provider.list_machine_ids()]
        return self._records

    def list_machine_ids(self) -> list[str]:
        return [item.machine_id for item in self._all()]

    def get_version(self) -> str: return "file"
    def get_many(self, machine_ids: Sequence[str]) -> list[MachineRecord]: return [m for m in self._all() if m.machine_id in set(machine_ids)]
    def search(self, query: MachineSearchQuery) -> MachinePage:
        items = self._filter(self._all(), query)
        start = next((i + 1 for i, item in enumerate(items) if item.machine_id == decode_cursor(query.cursor)), 0)
        page = items[start:start + min(max(query.limit, 1), 200)]
        return MachinePage(page, encode_cursor(page[-1].machine_id) if start + len(page) < len(items) else None, "file", len(items))
    def get_facets(self, query: MachineSearchQuery) -> MachineFacets:
        items = self._filter(self._all(), query)
        return MachineFacets(_counts(items, "resource_cohort"), _counts(items, "status"), _counts(items, "backfill_eligible"))
    @staticmethod
    def _filter(items: list[MachineRecord], query: MachineSearchQuery) -> list[MachineRecord]:
        needle = query.search.lower().strip()
        return [m for m in items if (not needle or needle in m.machine_id.lower() or needle in m.display_name.lower()) and (not query.cohort or m.resource_cohort == query.cohort) and (not query.status or m.status == query.status) and (query.eligible is None or m.backfill_eligible == query.eligible)]


def _counts(items: list[MachineRecord], field: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        key = str(getattr(item, field)).lower()
        result[key] = result.get(key, 0) + 1
    return result


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_iso(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)
