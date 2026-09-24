"""Durable, per-machine/month control records for ULRPM backfills.

The dashboard deliberately keeps this separate from its command-action ledger:
an
action records what the dashboard submitted, while this store is the durable
coordination point used by the parent flow and by replacement submissions.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .manifests import orchestrator_month_index, validate_machine_id
from .months import current_month_index, month_for_index

ACTIVE_MONTH_STATES = {"queued", "running", "cancel_requested", "stopping", "requeue_pending"}
TERMINAL_MONTH_STATES = {"completed", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _month_fields(month_index: int) -> tuple[int, int]:
    if not isinstance(month_index, int) or not 0 <= month_index <= current_month_index():
        raise ValueError("month_index must be a valid ULRPM month index through today")
    return month_for_index(month_index)


class BackfillControlStore(Protocol):
    def get_month_state(self, machine_id: str, month_index: int) -> dict | None: ...
    def list_machine_states(self, machine_id: str) -> list[dict]: ...
    def request_cancel(self, machine_id: str, month_index: int, parent_workflow_id: str, child_workflow_id: str, requested_by: str) -> dict: ...
    def mark_requeue_requested(self, machine_id: str, month_index: int, parent_workflow_id: str, requested_by: str, *, action_id: str | None = None) -> dict: ...
    def reserve_month(self, machine_id: str, month_index: int, parent_workflow_id: str, requested_by: str) -> dict: ...
    def link_parent_workflow(self, machine_id: str, month_index: int, workflow_id: str) -> dict: ...
    def complete_action(self, machine_id: str, month_index: int, state: str, *, replacement_workflow_id: str | None = None, error: str | None = None) -> dict: ...


class JsonControlStore:
    """JSON implementation used for local tests and local-only dashboard runs."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def _key(self, machine_id: str, month_index: int) -> str:
        return f"{validate_machine_id(machine_id)}:{month_index}"

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _save(self, records: dict[str, dict]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")

    def get_month_state(self, machine_id: str, month_index: int) -> dict | None:
        _month_fields(month_index)
        return self._load().get(self._key(machine_id, month_index))

    def list_machine_states(self, machine_id: str) -> list[dict]:
        machine_id = validate_machine_id(machine_id)
        return sorted((record for record in self._load().values() if record["machine_id"] == machine_id), key=lambda record: record["month_index"])

    def _upsert(self, machine_id: str, month_index: int, mutate) -> dict:
        year, month = _month_fields(month_index)
        key = self._key(machine_id, month_index)
        records = self._load()
        record = records.get(key, {"machine_id": validate_machine_id(machine_id), "month_index": month_index, "year": year, "month": month, "events": [], "created_at": _now()})
        mutate(record)
        record["updated_at"] = _now()
        records[key] = record
        self._save(records)
        return record

    def reserve_month(self, machine_id: str, month_index: int, parent_workflow_id: str, requested_by: str) -> dict:
        existing = self.get_month_state(machine_id, month_index)
        if existing and existing.get("state") in ACTIVE_MONTH_STATES:
            raise ValueError(f"{existing['year']}-{existing['month']:02d} is already {existing['state']}")
        return self._upsert(machine_id, month_index, lambda record: record.update({"state": "queued", "action": "backfill", "parent_workflow_id": parent_workflow_id, "requested_by": requested_by, "events": [*record["events"], {"state": "queued", "at": _now(), "actor": requested_by}]}))

    def link_parent_workflow(self, machine_id: str, month_index: int, workflow_id: str) -> dict:
        """Replace an ephemeral dashboard action id with the durable Argo id."""
        return self._upsert(machine_id, month_index, lambda record: record.update({"parent_workflow_id": workflow_id, "events": [*record["events"], {"state": "parent_linked", "at": _now(), "workflow_id": workflow_id}]}))

    def request_cancel(self, machine_id: str, month_index: int, parent_workflow_id: str, child_workflow_id: str, requested_by: str) -> dict:
        return self._upsert(machine_id, month_index, lambda record: record.update({"state": "cancel_requested", "action": "cancel", "parent_workflow_id": parent_workflow_id, "child_workflow_id": child_workflow_id or record.get("child_workflow_id", ""), "requested_by": requested_by, "events": [*record["events"], {"state": "cancel_requested", "at": _now(), "actor": requested_by}]}))

    def mark_requeue_requested(self, machine_id: str, month_index: int, parent_workflow_id: str, requested_by: str, *, action_id: str | None = None) -> dict:
        return self._upsert(machine_id, month_index, lambda record: record.update({"state": "requeue_pending", "action": "requeue", "parent_workflow_id": parent_workflow_id, "requested_by": requested_by, "action_id": action_id, "events": [*record["events"], {"state": "requeue_pending", "at": _now(), "actor": requested_by}]}))

    def complete_action(self, machine_id: str, month_index: int, state: str, *, replacement_workflow_id: str | None = None, error: str | None = None) -> dict:
        if state not in TERMINAL_MONTH_STATES | {"queued", "running", "stopping"}:
            raise ValueError(f"invalid month state: {state}")
        return self._upsert(machine_id, month_index, lambda record: record.update({"state": state, "replacement_workflow_id": replacement_workflow_id or record.get("replacement_workflow_id"), "error": error, "events": [*record["events"], {"state": state, "at": _now(), **({"error": error} if error else {})}]}))


class AzureBlobControlStore(JsonControlStore):
    """Azure Blob JSON adapter at ``dashboard-control/<machine>/month_<n>.json``.

    It uses the configured backfill account and only initializes Azure clients on
    an actual control operation, keeping the read-only dashboard start-up safe.
    """

    def __init__(self, account_name: str, container_name: str) -> None:
        super().__init__(Path("/nonexistent"))
        self.account_name = account_name
        self.container_name = container_name

    def _blob(self, machine_id: str, month_index: int):
        from azure.storage.blob import BlobClient
        from .credentials import ManifestCredentialResolver
        return BlobClient(
            account_url=f"https://{self.account_name}.blob.core.windows.net",
            container_name=self.container_name,
            blob_name=f"dashboard-control/{validate_machine_id(machine_id)}/month_{month_index}.json",
            credential=ManifestCredentialResolver().resolve().value,
        )

    def get_month_state(self, machine_id: str, month_index: int) -> dict | None:
        _month_fields(month_index)
        try:
            return json.loads(self._blob(machine_id, month_index).download_blob().readall())
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404 or getattr(exc, "error_code", None) == "BlobNotFound":
                return None
            raise

    def list_machine_states(self, machine_id: str) -> list[dict]:
        from azure.storage.blob import ContainerClient
        from .credentials import ManifestCredentialResolver
        container = ContainerClient(f"https://{self.account_name}.blob.core.windows.net", self.container_name, credential=ManifestCredentialResolver().resolve().value)
        prefix = f"dashboard-control/{validate_machine_id(machine_id)}/"
        return sorted((json.loads(container.get_blob_client(blob.name).download_blob().readall()) for blob in container.list_blobs(name_starts_with=prefix)), key=lambda record: record["month_index"])

    def _upsert(self, machine_id: str, month_index: int, mutate) -> dict:
        # The parent flow is the sole writer after cancellation is requested;
        # dashboard operations are idempotent by state and overwrite the same key.
        year, month = _month_fields(month_index)
        record = self.get_month_state(machine_id, month_index) or {"machine_id": validate_machine_id(machine_id), "month_index": month_index, "year": year, "month": month, "events": [], "created_at": _now()}
        mutate(record)
        record["updated_at"] = _now()
        self._blob(machine_id, month_index).upload_blob(json.dumps(record, sort_keys=True), overwrite=True)
        return record


class DatabricksControlStore(JsonControlStore):
    """Unity Catalog control-table adapter.

    The table is intentionally narrow (machine_id, month_index, record_json)
    so the immutable record schema can evolve without a dashboard migration.
    Provision the table with a unique logical key and grant the dashboard
    service principal SELECT/INSERT/UPDATE before switching environments.
    """

    def __init__(self, table: str) -> None:
        super().__init__(Path("/nonexistent"))
        if not table.replace("_", "").replace(".", "").isalnum():
            raise ValueError("BACKFILL_CONTROL_TABLE must be a qualified safe table name")
        self.table = table

    def _connect(self):
        from databricks import sql
        return sql.connect(
            server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
            http_path=os.environ["DATABRICKS_HTTP_PATH"],
            access_token=os.environ["DATABRICKS_TOKEN"],
        )

    def _rows(self, statement: str) -> list[tuple]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statement)
            return cursor.fetchall()

    def get_month_state(self, machine_id: str, month_index: int) -> dict | None:
        _month_fields(month_index)
        machine_id = validate_machine_id(machine_id)
        rows = self._rows(f"SELECT record_json FROM {self.table} WHERE machine_id = '{machine_id}' AND month_index = {month_index}")
        return json.loads(rows[0][0]) if rows else None

    def list_machine_states(self, machine_id: str) -> list[dict]:
        machine_id = validate_machine_id(machine_id)
        return [json.loads(row[0]) for row in self._rows(f"SELECT record_json FROM {self.table} WHERE machine_id = '{machine_id}' ORDER BY month_index")]

    def _upsert(self, machine_id: str, month_index: int, mutate) -> dict:
        year, month = _month_fields(month_index)
        machine_id = validate_machine_id(machine_id)
        record = self.get_month_state(machine_id, month_index) or {"machine_id": machine_id, "month_index": month_index, "year": year, "month": month, "events": [], "created_at": _now()}
        mutate(record)
        record["updated_at"] = _now()
        payload = json.dumps(record).replace("'", "''")
        statement = (
            f"MERGE INTO {self.table} target USING (SELECT '{machine_id}' machine_id, {month_index} month_index, '{payload}' record_json) source "
            "ON target.machine_id = source.machine_id AND target.month_index = source.month_index "
            "WHEN MATCHED THEN UPDATE SET record_json = source.record_json "
            "WHEN NOT MATCHED THEN INSERT (machine_id, month_index, record_json) VALUES (source.machine_id, source.month_index, source.record_json)"
        )
        self._rows(statement)
        return record


def build_control_store(*, state_path: Path) -> BackfillControlStore:
    kind = os.getenv("BACKFILL_CONTROL_STORE", "azure_blob").lower()
    if kind == "local_json":
        return JsonControlStore(state_path)
    if kind == "azure_blob":
        return AzureBlobControlStore(os.getenv("FST_BACKFILL_ACCOUNT_NAME", "aifleetmlopsfstbackfill"), os.getenv("FST_BACKFILL_CONTAINER_NAME", "fst-backfill"))
    if kind == "databricks":
        return DatabricksControlStore(os.getenv("BACKFILL_CONTROL_TABLE", "dih_prod.mlops.backfill_dashboard_control"))
    raise RuntimeError("BACKFILL_CONTROL_STORE must be azure_blob, databricks, or local_json")
