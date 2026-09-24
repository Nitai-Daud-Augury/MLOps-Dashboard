from __future__ import annotations

import io
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .credentials import ManifestCredentialResolver
from .months import (
    current_month_index,
    month_for_index,
    month_window_for_index,
    orchestrator_month_index as stable_orchestrator_month_index,
    orchestrator_months as dynamic_orchestrator_months,
)


DEFAULT_MANIFEST_ACCOUNT = "aifleetmlopsfstbackfill"
DEFAULT_MANIFEST_CONTAINER = "fst-backfill"
@dataclass(frozen=True)
class MachineManifestRequest:
    machine_id: str
    since: str
    until: str
    manifest_path: str = ""


@dataclass(frozen=True)
class MultiMachineManifestRequest:
    machine_ids: list[str]
    since: str
    until: str
    manifest_path: str = ""


@dataclass(frozen=True)
class ManifestWriteResult:
    account_name: str
    container_name: str
    manifest_path: str
    rows: int
    blob_url: str


@dataclass(frozen=True)
class OrchestratedManifestWriteResult:
    account_name: str
    container_name: str
    manifest_prefix: str
    manifest_template: str
    machine_ids: list[str]
    start_index: int
    end_index: int
    month_count: int
    manifest_count: int
    blob_url: str
    month_indices_by_machine: dict[str, list[int]]


class BackfillManifestWriter:
    def __init__(
        self,
        *,
        account_name: str | None = None,
        container_name: str | None = None,
        credential_resolver: ManifestCredentialResolver | None = None,
    ) -> None:
        self.account_name = account_name or os.getenv("FST_BACKFILL_ACCOUNT_NAME", DEFAULT_MANIFEST_ACCOUNT)
        self.container_name = container_name or os.getenv("FST_BACKFILL_CONTAINER_NAME", DEFAULT_MANIFEST_CONTAINER)
        self.credential_resolver = credential_resolver or ManifestCredentialResolver()

    def readiness(self) -> dict[str, str | bool]:
        credential = self.credential_resolver.resolve()
        try:
            self._container().get_container_properties()
        except Exception as exc:
            error_code = getattr(exc, "error_code", None)
            detail = error_code or exc.__class__.__name__
            raise RuntimeError(
                f"Manifest upload is unavailable: cannot access {self.account_name}/{self.container_name} "
                f"using the {credential.source} credential ({detail})."
            ) from exc
        return {
            "account_name": self.account_name,
            "container_name": self.container_name,
            "ready": True,
            "credential_source": credential.source,
            "credential_kind": credential.kind,
        }

    def write_machine_manifest(self, request: MachineManifestRequest) -> ManifestWriteResult:
        return self.write_multi_machine_manifest(
            MultiMachineManifestRequest(
                machine_ids=[request.machine_id],
                since=request.since,
                until=request.until,
                manifest_path=request.manifest_path.strip() or machine_manifest_path(request),
            )
        )

    def write_multi_machine_manifest(self, request: MultiMachineManifestRequest) -> ManifestWriteResult:
        manifest_path = request.manifest_path.strip() or multi_machine_manifest_path(request)
        validate_manifest_path(manifest_path)
        since = validate_timestamp(request.since, field_name="since")
        until = validate_timestamp(request.until, field_name="until")
        if until <= since:
            raise ValueError("until must be after since")
        machine_ids = validate_machine_ids(request.machine_ids)
        content = _parquet_bytes(
            machine_ids=machine_ids,
            since=normalized_timestamp(since),
            until=normalized_timestamp(until),
        )
        blob = self._container().get_blob_client(manifest_path)
        blob.upload_blob(content, overwrite=True)
        return ManifestWriteResult(
            account_name=self.account_name,
            container_name=self.container_name,
            manifest_path=manifest_path,
            rows=len(machine_ids),
            blob_url=f"https://{self.account_name}.blob.core.windows.net/{self.container_name}/{manifest_path}",
        )

    def write_orchestrated_monthly_manifests(
        self,
        *,
        machine_ids: list[str],
        since: str,
        until: str,
        manifest_prefix: str = "",
        month_indices_by_machine: dict[str, list[int]] | None = None,
    ) -> OrchestratedManifestWriteResult:
        validated_machine_ids = validate_machine_ids(machine_ids)
        if month_indices_by_machine:
            if set(month_indices_by_machine) != set(validated_machine_ids):
                raise ValueError("month_indices_by_machine must contain exactly the requested machine IDs")
            normalized_indices = {
                machine_id: _validate_orchestrator_indices(month_indices_by_machine[machine_id])
                for machine_id in validated_machine_ids
            }
        else:
            start = validate_timestamp(since, field_name="since")
            end = validate_timestamp(until, field_name="until")
            if end <= start:
                raise ValueError("until must be after since")
            shared_indices = [index for index, *_ in orchestrator_month_windows(start, end)]
            normalized_indices = {machine_id: shared_indices for machine_id in validated_machine_ids}
        prefix = validate_manifest_prefix(manifest_prefix) if manifest_prefix.strip() else orchestrator_manifest_prefix()
        template = (
            f"{prefix}/monthly_{{machine_short}}_{{machine_id}}/"
            "month_{month_index:02d}_{year}_{month:02d}.parquet"
        )
        container = self._container()
        now = datetime.now(timezone.utc)
        for machine_id in validated_machine_ids:
            for month_index in normalized_indices[machine_id]:
                year, month = month_for_index(month_index)
                month_since, month_until = month_window_for_index(month_index, now)
                manifest_path = template.format(
                    machine_id=machine_id,
                    machine_short=machine_id[:4],
                    month_index=month_index,
                    year=year,
                    month=month,
                )
                container.get_blob_client(manifest_path).upload_blob(
                    _parquet_bytes(
                        machine_ids=[machine_id],
                        since=normalized_timestamp(month_since),
                        until=normalized_timestamp(month_until),
                    ),
                    overwrite=True,
                )

        return OrchestratedManifestWriteResult(
            account_name=self.account_name,
            container_name=self.container_name,
            manifest_prefix=prefix,
            manifest_template=template,
            machine_ids=validated_machine_ids,
            start_index=min(index for indices in normalized_indices.values() for index in indices),
            end_index=max(index for indices in normalized_indices.values() for index in indices) + 1,
            month_count=sum(len(indices) for indices in normalized_indices.values()),
            manifest_count=sum(len(indices) for indices in normalized_indices.values()),
            blob_url=f"https://{self.account_name}.blob.core.windows.net/{self.container_name}/{prefix}",
            month_indices_by_machine=normalized_indices,
        )

    def _container(self):
        try:
            from azure.storage.blob import ContainerClient
        except ImportError as exc:
            raise RuntimeError(
                "Azure dependencies are missing. Install azure-identity and azure-storage-blob "
                "to upload backfill manifests."
            ) from exc

        account_url = f"https://{self.account_name}.blob.core.windows.net"
        credential = self.credential_resolver.resolve()
        return ContainerClient(account_url, self.container_name, credential=credential.value)


def machine_manifest_path(request: MachineManifestRequest) -> str:
    machine_id = validate_machine_id(request.machine_id)
    since = validate_timestamp(request.since, field_name="since")
    until = validate_timestamp(request.until, field_name="until")
    if until <= since:
        raise ValueError("until must be after since")
    generated_at = datetime.now(timezone.utc).strftime("%Y/%m/%d/dashboard/single-machine")
    since_label = since.strftime("%Y%m%d")
    until_label = until.strftime("%Y%m%d")
    return f"{generated_at}/{machine_id}_{since_label}_{until_label}.parquet"


def multi_machine_manifest_path(request: MultiMachineManifestRequest) -> str:
    machine_ids = validate_machine_ids(request.machine_ids)
    since = validate_timestamp(request.since, field_name="since")
    until = validate_timestamp(request.until, field_name="until")
    if until <= since:
        raise ValueError("until must be after since")
    # Include a UTC timestamp so repeated dashboard submissions do not overwrite a
    # prior manifest for the same machine set and date range.
    generated_at = datetime.now(timezone.utc).strftime("%Y/%m/%d/dashboard/multi-machine/%H%M%S_%f")
    return (
        f"{generated_at}/{len(machine_ids)}_machines_"
        f"{since.strftime('%Y%m%d')}_{until.strftime('%Y%m%d')}.parquet"
    )


def validate_machine_id(machine_id: str) -> str:
    value = machine_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{6,128}", value):
        raise ValueError("machine_id must be 6-128 URL-safe characters")
    return value


def validate_machine_ids(machine_ids: list[str]) -> list[str]:
    if not machine_ids:
        raise ValueError("At least one machine_id is required")
    deduplicated = list(dict.fromkeys(validate_machine_id(machine_id) for machine_id in machine_ids))
    if len(deduplicated) > 500:
        raise ValueError("A manifest can contain at most 500 machines")
    return deduplicated


def validate_manifest_path(path: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_./=-]+\.parquet", path):
        raise ValueError("manifest_path must be a relative parquet path with safe characters")
    if path.startswith("/") or ".." in path.split("/"):
        raise ValueError("manifest_path must stay relative and cannot contain '..'")
    return path


def validate_manifest_prefix(path: str) -> str:
    value = path.strip().strip("/")
    if not value or not re.fullmatch(r"[A-Za-z0-9_./=-]+", value):
        raise ValueError("manifest_prefix must be a relative path with safe characters")
    if ".." in value.split("/"):
        raise ValueError("manifest_prefix must stay relative and cannot contain '..'")
    return value


def validate_timestamp(value: str, *, field_name: str) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp or date") from exc
    return parsed


def normalized_timestamp(value: datetime) -> str:
    """The dev FST seeder requires the legacy slash-separated timestamp format."""
    return value.strftime("%Y/%m/%d/%H")


def orchestrator_months() -> list[tuple[int, int]]:
    return dynamic_orchestrator_months()


def orchestrator_month_index(year: int, month: int) -> int:
    """Return the stable index for a month no later than the current month."""
    index = stable_orchestrator_month_index(year, month)
    if index > current_month_index():
        raise ValueError(
            f"{year}-{month:02d} is later than the current ULRPM month."
        )
    return index


def orchestrator_month_windows(start: datetime, end: datetime) -> list[tuple[int, int, int, datetime, datetime]]:
    now = datetime.now(start.tzinfo) if start.tzinfo else datetime.now()
    effective_end = min(end, now)
    windows: list[tuple[int, int, int, datetime, datetime]] = []
    months = dynamic_orchestrator_months(now)
    for index, (year, month) in enumerate(months):
        month_start = datetime(year, month, 1, tzinfo=start.tzinfo)
        next_month = datetime(year + 1, 1, 1, tzinfo=start.tzinfo) if month == 12 else datetime(year, month + 1, 1, tzinfo=start.tzinfo)
        if start < next_month and effective_end > month_start:
            windows.append((index, year, month, max(start, month_start), min(effective_end, next_month)))
    if not windows:
        first_year, first_month = months[0]
        last_year, last_month = months[-1]
        raise ValueError(
            "The ULRPM orchestrator supports ranges overlapping "
            f"{first_year}-{first_month:02d} through {last_year}-{last_month:02d}."
        )
    return windows


def _validate_orchestrator_indices(indices: list[int]) -> list[int]:
    last_index = current_month_index()
    if not indices or any(not isinstance(index, int) or index < 0 or index > last_index for index in indices):
        raise ValueError("month indices must be non-empty valid orchestrator month indices")
    if len(set(indices)) != len(indices):
        raise ValueError("month indices cannot contain duplicates")
    return sorted(indices)


def orchestrator_manifest_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y/%m/%d/dashboard/orchestrator/%H%M%S_%f")


def manifest_command_preview(result: ManifestWriteResult) -> list[str]:
    return ["upload-machine-manifest", result.manifest_path]


def manifest_stdout(result: ManifestWriteResult) -> str:
    return json.dumps(asdict(result), indent=2, sort_keys=True)


def _parquet_bytes(*, machine_ids: list[str], since: str, until: str) -> bytes:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required to create parquet manifests") from exc

    table = pa.table(
        {
            "machine_id": machine_ids,
            "since": [since] * len(machine_ids),
            "until": [until] * len(machine_ids),
        }
    )
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()
