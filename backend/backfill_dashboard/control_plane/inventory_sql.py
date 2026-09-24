from __future__ import annotations

import json
import base64
from dataclasses import asdict

from ..inventory_models import MachineRecord, MachineSearchQuery

FIELDS = tuple(MachineRecord.__dataclass_fields__)


def record_values(record: MachineRecord) -> tuple:
    data = asdict(record)
    for field in ("tags", "endpoint_hardware_types", "classification_reason"):
        data[field] = json.dumps(data[field], separators=(",", ":"))
    data["archived"] = int(data["archived"])
    data["backfill_eligible"] = int(data["backfill_eligible"])
    data["is_test_machine"] = int(data["is_test_machine"])
    return tuple(data[field] for field in FIELDS)


def row_record(row) -> MachineRecord:
    data = dict(row)
    for field in ("tags", "endpoint_hardware_types", "classification_reason"):
        data[field] = tuple(json.loads(data[field]))
    data["archived"] = bool(data["archived"])
    data["backfill_eligible"] = bool(data["backfill_eligible"])
    data["is_test_machine"] = bool(data["is_test_machine"])
    return MachineRecord(**data)


def query_where(query: MachineSearchQuery) -> tuple[list[str], list]:
    where: list[str] = []
    values: list = []
    if query.search:
        where.append("(machine_id LIKE ? OR display_name LIKE ?)")
        needle = f"%{query.search.strip()}%"
        values.extend((needle, needle))
    for field, value in (("resource_cohort", query.cohort), ("status", query.status)):
        if value:
            where.append(f"{field}=?")
            values.append(value)
    if query.eligible is not None:
        where.append("backfill_eligible=?")
        values.append(int(query.eligible))
    for field, value in (("site_id", query.site_id), ("organization_id", query.organization_id)):
        if value:
            where.append(f"{field}=?")
            values.append(value)
    if query.classification_issue:
        where.append("classification_reason LIKE ?")
        values.append(f"%{query.classification_issue}%")
    return where, values


def sort_spec(query: MachineSearchQuery) -> tuple[str, str]:
    fields = {"machine_id", "display_name", "status", "resource_cohort", "site_name", "organization_name"}
    if query.sort_by not in fields or query.sort_dir not in {"asc", "desc"}:
        raise ValueError("invalid inventory sort")
    return query.sort_by, query.sort_dir.upper()


def encode_seek(value: str, machine_id: str) -> str:
    payload = json.dumps({"value": value, "machine_id": machine_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_seek(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        return str(payload["value"]), str(payload["machine_id"])
    except Exception as exc:
        raise ValueError("invalid inventory cursor") from exc
