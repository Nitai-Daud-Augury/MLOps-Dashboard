"""Pure campaign planning primitives used by durable scheduler adapters."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Literal

from .inventory_models import MachineRecord

WorkState = Literal["blocked", "ready"]


@dataclass(frozen=True)
class WorkItem:
    idempotency_key: str
    campaign_id: str
    machine_id: str
    cohort: str
    window_start: date
    window_end: date
    sequence_number: int
    state: WorkState
    classifier_version: str


def idempotency_key(campaign_id: str, machine_id: str, start_at: date, end_at: date, feature_set_version: str, config_digest: str) -> str:
    value = "|".join((campaign_id, machine_id, start_at.isoformat(), end_at.isoformat(), feature_set_version, config_digest))
    return hashlib.sha256(value.encode()).hexdigest()


def plan_machine_windows(machine: MachineRecord, *, campaign_id: str, start_at: date, end_at: date, window_days: int, feature_set_version: str, config_digest: str) -> list[WorkItem]:
    if machine.resource_cohort == "unknown" or not machine.backfill_eligible:
        raise ValueError(f"machine {machine.machine_id} is not eligible for campaign scheduling")
    if end_at <= start_at or window_days < 1:
        raise ValueError("invalid campaign date range or window size")
    result: list[WorkItem] = []
    current = start_at
    sequence = 0
    while current < end_at:
        window_end = min(end_at, current + timedelta(days=window_days))
        result.append(WorkItem(idempotency_key(campaign_id, machine.machine_id, current, window_end, feature_set_version, config_digest), campaign_id, machine.machine_id, machine.resource_cohort, current, window_end, sequence, "ready" if sequence == 0 else "blocked", machine.classification_source_version))
        current, sequence = window_end, sequence + 1
    return result


def resolve_selection(records: Iterable[MachineRecord], *, eligible: bool | None = None, cohort: str | None = None, excluded_machine_ids: set[str] | None = None) -> list[MachineRecord]:
    excluded = excluded_machine_ids or set()
    return [record for record in records if record.machine_id not in excluded and (eligible is None or record.backfill_eligible == eligible) and (cohort is None or record.resource_cohort == cohort)]
