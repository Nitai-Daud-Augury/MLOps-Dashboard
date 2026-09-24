from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .classification import ResourceCohort


@dataclass(frozen=True)
class MachineRecord:
    machine_id: str
    display_name: str = ""
    is_test_machine: bool = False
    site_id: str | None = None
    site_name: str | None = None
    organization_id: str | None = None
    organization_name: str | None = None
    status: str = "unknown"
    archived: bool = False
    tags: tuple[str, ...] = ()
    endpoint_count: int = 0
    endpoint_hardware_types: tuple[str, ...] = ()
    resource_cohort: ResourceCohort = "unknown"
    classification_reason: tuple[str, ...] = ()
    classification_source_version: str = ""
    backfill_eligible: bool = False
    exclusion_reason: str | None = None
    source_updated_at: str | None = None
    last_recorded_at: str | None = None
    installation_at: str | None = None
    inventory_version: str = ""


@dataclass(frozen=True)
class MachineSearchQuery:
    cursor: str | None = None
    limit: int = 100
    search: str = ""
    cohort: ResourceCohort | None = None
    status: str | None = None
    eligible: bool | None = None
    site_id: str | None = None
    organization_id: str | None = None
    classification_issue: str | None = None
    sort_by: str = "machine_id"
    sort_dir: str = "asc"


@dataclass(frozen=True)
class MachinePage:
    machines: list[MachineRecord]
    next_cursor: str | None
    inventory_version: str
    total_estimate: int | None = None


@dataclass(frozen=True)
class MachineFacets:
    cohorts: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    eligible: dict[str, int] = field(default_factory=dict)
    sites: dict[str, int] = field(default_factory=dict)
    organizations: dict[str, int] = field(default_factory=dict)
    classification_issues: dict[str, int] = field(default_factory=dict)
