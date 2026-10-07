from __future__ import annotations

import io
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .credentials import ManifestCredentialResolver
from .months import (
    current_month_index,
    month_for_index,
    month_window_for_index,
    orchestrator_month_index as stable_orchestrator_month_index,
    orchestrator_months as dynamic_orchestrator_months,
)
from .windows import (
    ManifestWindow,
    SplitSpec,
    enforce_per_machine_cap,
    month_windows as utc_month_windows,
    split_windows,
    windows_for_selection,
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
    windows_by_machine: dict[str, list[dict]]
    split: dict


@dataclass(frozen=True)
class DailyGapManifestWriteResult:
    account_name: str
    container_name: str
    manifest_prefix: str
    machine_id: str
    month_count: int
    day_count: int
    manifests: list[dict[str, str]]
    blob_url: str


@dataclass(frozen=True)
class PlannedManifest:
    machine_id: str
    window: ManifestWindow
    manifest_path: str


class LocalDirectorySink:
    """Write manifest bytes under a local root. Never touches Azure credentials."""

    def __init__(self, root: "Path") -> None:
        from pathlib import Path
        self.root = Path(root)

    def write(self, manifest_path: str, content: bytes, *, overwrite: bool = True) -> None:
        target = self.root / manifest_path
        if target.exists() and not overwrite:
            raise FileExistsError(manifest_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


class _AzureContainerSink:
    def __init__(self, container) -> None:
        self._container = container

    def write(self, manifest_path: str, content: bytes, *, overwrite: bool = True) -> None:
        self._container.get_blob_client(manifest_path).upload_blob(content, overwrite=overwrite)


def plan_orchestrated_manifests(
    *,
    machine_ids: list[str],
    windows_by_machine: dict[str, list[ManifestWindow]],
    prefix: str,
    split: SplitSpec,
) -> list[PlannedManifest]:
    planned: list[PlannedManifest] = []
    for machine_id in machine_ids:
        windows = windows_by_machine.get(machine_id, [])
        for window in windows:
            use_legacy = window.chunk_count == 1 and split.mode == "month"
            if use_legacy:
                manifest_path = (
                    f"{prefix}/monthly_{machine_id[:4]}_{machine_id}/"
                    f"month_{window.month_index:02d}_{window.year}_{window.month:02d}.parquet"
                )
            else:
                since_label = window.since.strftime("%Y%m%d%H")
                until_label = window.until.strftime("%Y%m%d%H")
                manifest_path = (
                    f"{prefix}/monthly_{machine_id[:4]}_{machine_id}/"
                    f"month_{window.month_index:02d}_{window.year}_{window.month:02d}/"
                    f"part_{window.chunk_index:03d}_{since_label}_{until_label}.parquet"
                )
            planned.append(
                PlannedManifest(
                    machine_id=machine_id,
                    window=window,
                    manifest_path=validate_manifest_path(manifest_path),
                )
            )
    return planned


def render_manifest_table(planned: PlannedManifest):
    try:
        import pyarrow as pa
    except ImportError as exc:
        raise RuntimeError("pyarrow is required to create parquet manifests") from exc
    return pa.table(
        {
            "machine_id": [planned.machine_id],
            "since": [normalized_timestamp(planned.window.since)],
            "until": [normalized_timestamp(planned.window.until)],
        }
    )


def write_planned(planned: list[PlannedManifest], sink, *, overwrite: bool = True) -> None:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required to create parquet manifests") from exc
    import io
    for item in planned:
        table = render_manifest_table(item)
        buf = io.BytesIO()
        pq.write_table(table, buf)
        sink.write(item.manifest_path, buf.getvalue(), overwrite=overwrite)


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
        split: SplitSpec | dict | None = None,
        now: datetime | None = None,
        sink=None,
    ) -> OrchestratedManifestWriteResult:
        validated_machine_ids = validate_machine_ids(machine_ids)
        split_spec = SplitSpec.from_mapping(split).validated()
        current = now or datetime.now(timezone.utc)
        current = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
        if month_indices_by_machine is not None:
            if set(month_indices_by_machine) != set(validated_machine_ids):
                raise ValueError("month_indices_by_machine must contain exactly the requested machine IDs")
            for machine_id in validated_machine_ids:
                _validate_orchestrator_indices(month_indices_by_machine[machine_id], now=current)
        windows_by_machine: dict[str, list[ManifestWindow]] = {}
        for machine_id in validated_machine_ids:
            indices = None if month_indices_by_machine is None else month_indices_by_machine[machine_id]
            windows = windows_for_selection(
                since=since,
                until=until,
                month_indices=indices,
                split=split_spec,
                now=current,
            )
            enforce_per_machine_cap(windows, machine_id=machine_id)
            windows_by_machine[machine_id] = windows
        if not any(windows_by_machine.values()):
            raise ValueError("No manifests matched the requested since/until and month selection")
        prefix = validate_manifest_prefix(manifest_prefix) if manifest_prefix.strip() else orchestrator_manifest_prefix()
        template = (
            f"{prefix}/monthly_{{machine_short}}_{{machine_id}}/"
            "month_{month_index:02d}_{year}_{month:02d}.parquet"
        )
        planned = plan_orchestrated_manifests(
            machine_ids=validated_machine_ids,
            windows_by_machine=windows_by_machine,
            prefix=prefix,
            split=split_spec,
        )
        if sink is None:
            sink = _AzureContainerSink(self._container())
        write_planned(planned, sink, overwrite=True)
        normalized_indices = {
            machine_id: sorted({window.month_index for window in windows})
            for machine_id, windows in windows_by_machine.items()
            if windows
        }
        # Machines with empty windows after filter are dropped from the index map.
        machine_ids_with_windows = [mid for mid in validated_machine_ids if normalized_indices.get(mid)]
        if not machine_ids_with_windows:
            raise ValueError("No manifests matched the requested since/until and month selection")
        windows_payload = {
            machine_id: [
                {
                    "month_index": window.month_index,
                    "year": window.year,
                    "month": window.month,
                    "chunk_index": window.chunk_index,
                    "chunk_count": window.chunk_count,
                    "since": normalized_timestamp(window.since),
                    "until": normalized_timestamp(window.until),
                    "manifest_path": next(
                        item.manifest_path
                        for item in planned
                        if item.machine_id == machine_id and item.window is window
                    ),
                }
                for window in windows
            ]
            for machine_id, windows in windows_by_machine.items()
            if windows
        }
        all_indices = [index for indices in normalized_indices.values() for index in indices]
        return OrchestratedManifestWriteResult(
            account_name=self.account_name,
            container_name=self.container_name,
            manifest_prefix=prefix,
            manifest_template=template,
            machine_ids=machine_ids_with_windows,
            start_index=min(all_indices),
            end_index=max(all_indices) + 1,
            month_count=sum(len(indices) for indices in normalized_indices.values()),
            manifest_count=len(planned),
            blob_url=f"https://{self.account_name}.blob.core.windows.net/{self.container_name}/{prefix}",
            month_indices_by_machine=normalized_indices,
            windows_by_machine=windows_payload,
            split={"mode": split_spec.mode, "days": split_spec.days},
        )

    def write_daily_gap_manifests(
        self, *, machine_id: str, month_indices: list[int], manifest_prefix: str = "",
        now: datetime | None = None,
    ) -> DailyGapManifestWriteResult:
        machine_id = validate_machine_id(machine_id)
        current = now or datetime.now(timezone.utc)
        current = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
        indices = _validate_orchestrator_indices(month_indices, now=current)
        if not indices or len(indices) > 500:
            raise ValueError("month_indices must contain between 1 and 500 months")
        cutoff = datetime(current.year, current.month, current.day, tzinfo=timezone.utc)
        windows: list[tuple[datetime, datetime, int, int, int]] = []
        for index in indices:
            year, month = month_for_index(index)
            start, month_end = month_window_for_index(index, current)
            end = min(month_end, cutoff)
            if start >= end:
                continue
            for part in split_windows(start, end, SplitSpec(mode="day")):
                day, window_end = part.since, part.until
                next_day = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                if day.hour or day.minute or day.second or day.microsecond or window_end != next_day:
                    raise ValueError(f"Month {year}-{month:02d} does not contain only completed UTC days")
                windows.append((day, window_end, index, year, month))
        if not windows:
            raise ValueError("Selected months contain no completed UTC days")
        if len(windows) > 93:
            raise ValueError(
                f"Daily manifest request contains {len(windows)} days; select fewer gap months "
                "(maximum 93 completed days per request). No files were uploaded."
            )
        base = validate_manifest_prefix(manifest_prefix) if manifest_prefix.strip() else "daily-gaps"
        if len(base) > 400:
            raise ValueError("manifest_prefix is too long (maximum 400 characters)")
        unique_prefix = f"{base}/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')}_{uuid4().hex[:12]}"
        validate_manifest_prefix(unique_prefix)
        planned = []
        for start, end, index, year, month in windows:
            path = validate_manifest_path(
                f"{unique_prefix}/{machine_id}/{year:04d}/{month:02d}/{start:%Y%m%d}.parquet"
            )
            if len(path) > 1024:
                raise ValueError("Generated manifest path exceeds the Azure Blob name limit")
            planned.append((path, start, end))
        container = self._container()
        uploaded: list[dict[str, str]] = []
        for path, start, end in planned:
            try:
                container.get_blob_client(path).upload_blob(
                    _parquet_bytes(machine_ids=[machine_id], since=normalized_timestamp(start), until=normalized_timestamp(end)),
                    overwrite=False,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Daily manifest upload failed after {len(uploaded)} of {len(planned)} files; "
                    f"uploaded files: {[item['manifest_path'] for item in uploaded]}. No workflow was started."
                ) from exc
            uploaded.append({"manifest_path": path, "since": normalized_timestamp(start), "until": normalized_timestamp(end)})
        return DailyGapManifestWriteResult(
            account_name=self.account_name, container_name=self.container_name,
            manifest_prefix=unique_prefix, machine_id=machine_id,
            month_count=len({item[2] for item in windows}), day_count=len(uploaded), manifests=uploaded,
            blob_url=f"https://{self.account_name}.blob.core.windows.net/{self.container_name}/{unique_prefix}",
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
    """Thin wrapper kept for callers/tests; computation lives in windows.month_windows."""
    start_utc = start.astimezone(timezone.utc) if start.tzinfo else start.replace(tzinfo=timezone.utc)
    end_utc = end.astimezone(timezone.utc) if end.tzinfo else end.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    effective_end = min(end_utc, now)
    return utc_month_windows(start_utc, effective_end)


def _validate_orchestrator_indices(indices: list[int], *, now: datetime | None = None) -> list[int]:
    last_index = current_month_index(now)
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
