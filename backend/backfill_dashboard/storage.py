from __future__ import annotations

import hashlib
import io
import os
import re
import threading
from dataclasses import dataclass, field
from typing import BinaryIO, Protocol


class BlobNotFoundError(FileNotFoundError):
    pass


@dataclass(frozen=True)
class BlobObject:
    name: str
    content: bytes | BinaryIO
    url: str
    last_modified: str | None


class AzureBlobRangeReader(io.RawIOBase):
    """Seekable, read-only view of a blob backed by HTTP range requests.

    PyArrow seeks to the Parquet footer first and then fetches only the selected
    column chunks. Keeping this object seekable avoids downloading a multi-GB FST
    partition merely to inspect a handful of ultrasonic columns.
    """

    def __init__(self, blob_client: object, size: int) -> None:
        super().__init__()
        self._blob_client = blob_client
        self._size = max(0, int(size))
        self._position = 0
        self._lock = threading.Lock()
        self._parquet_use_threads = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        with self._lock:
            return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        with self._lock:
            if whence == io.SEEK_SET:
                position = offset
            elif whence == io.SEEK_CUR:
                position = self._position + offset
            elif whence == io.SEEK_END:
                position = self._size + offset
            else:
                raise ValueError(f"Unsupported seek mode: {whence}")
            if position < 0:
                raise ValueError("Cannot seek before the start of a blob")
            self._position = min(position, self._size)
            return self._position

    def read(self, size: int = -1) -> bytes:
        with self._lock:
            if self._position >= self._size or size == 0:
                return b""
            if size is None or size < 0:
                size = self._size - self._position
            length = min(int(size), self._size - self._position)
            offset = self._position
            content = self._blob_client.download_blob(  # type: ignore[attr-defined]
                offset=offset,
                length=length,
                max_concurrency=1,
            ).readall()
            self._position += len(content)
            return content

    def readinto(self, buffer: bytearray) -> int:
        content = self.read(len(buffer))
        buffer[:len(content)] = content
        return len(content)


class BlobStore(Protocol):
    def read_blob(self, blob_name: str) -> BlobObject:
        ...

    def read_parquet_metadata(self, blob_name: str) -> BlobObject:
        ...

    def list_blob_names(self, prefix: str) -> list[str]:
        ...


# ---------------------------------------------------------------------------
# Process-wide shared Azure Blob clients
# ---------------------------------------------------------------------------
# The FST scan fans out over ``BACKFILL_SCAN_WORKERS`` threads that all talk to
# the same storage account. azure-core's default RequestsTransport mounts a
# requests HTTPAdapter with urllib3's default ``pool_maxsize=10``; with more
# concurrent workers than that, urllib3 logs "Connection pool is full,
# discarding connection" and throws away keep-alive sockets (extra TLS
# handshakes). The API also built a fresh AzureBlobStore (and therefore a fresh
# ContainerClient + connection pool) for every scan request. We now build exactly
# one ContainerClient per (account, container, credential identity) for the whole
# process, backed by a requests.Session whose pool is sized from the configured
# worker count.

_DEFAULT_SCAN_WORKERS = 12
_POOL_HEADROOM = 4
_MIN_POOL_SIZE = 10  # never below urllib3/requests' default
_SHARED_CLIENT_LOCK = threading.Lock()
_SHARED_CONTAINER_CLIENTS: dict[tuple, object] = {}
_SHARED_DEFAULT_CREDENTIAL: object | None = None

_CLIENT_OPTIONS = {
    "connection_timeout": 10,
    "read_timeout": 60,
    "retry_total": 2,
    "retry_backoff_factor": 0.5,
}


def configured_scan_workers() -> int:
    """Worker count used by the scanner (``BACKFILL_SCAN_WORKERS``, default 12)."""
    try:
        return max(1, int(os.getenv("BACKFILL_SCAN_WORKERS", str(_DEFAULT_SCAN_WORKERS))))
    except ValueError:
        return _DEFAULT_SCAN_WORKERS


def blob_pool_size(scan_workers: int | None = None) -> int:
    """HTTP connection pool size for the shared blob client.

    Always ``>= scan_workers``: workers + small headroom for the coverage
    discovery / metadata calls that run alongside (floor 10, the old
    default). ``BACKFILL_BLOB_POOL_SIZE``
    may raise it further but can never shrink it below workers + headroom.
    """
    workers = max(1, int(scan_workers if scan_workers is not None else configured_scan_workers()))
    size = max(workers + _POOL_HEADROOM, _MIN_POOL_SIZE)
    override = os.getenv("BACKFILL_BLOB_POOL_SIZE", "").strip()
    if override:
        try:
            size = max(size, int(override))
        except ValueError:
            pass
    return size


def _credential_identity(account_key: str | None, sas_token: str | None) -> tuple[str, str]:
    """Stable, non-reversible identity of the credential, used only as a cache key.

    Only a short SHA-256 digest is kept so the secret never lands in a cache
    key, repr, or log line.
    """
    if account_key:
        return ("account_key", hashlib.sha256(account_key.encode("utf-8")).hexdigest()[:16])
    if sas_token:
        return ("sas_token", hashlib.sha256(sas_token.encode("utf-8")).hexdigest()[:16])
    return ("default_azure_credential", "")


def _build_pooled_transport(pool_size: int):
    import requests
    from urllib3.util.retry import Retry
    from azure.core.pipeline.transport import RequestsTransport

    try:
        from azure.core.pipeline.transport._bigger_block_size_http_adapters import (
            BiggerBlockSizeHTTPAdapter as _Adapter,
        )
    except ImportError:  # pragma: no cover - other azure-core layouts
        from requests.adapters import HTTPAdapter as _Adapter

    session = requests.Session()
    # Mirror RequestsTransport._init_session: azure-core owns retries via its
    # RetryPolicy, so the adapter itself must not retry.
    adapter = _Adapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=Retry(total=False, redirect=False, raise_on_status=False),
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    # session_owner=False: closing one client must never tear down the shared
    # session used by every scanner worker.
    return RequestsTransport(
        session=session,
        session_owner=False,
        connection_timeout=_CLIENT_OPTIONS["connection_timeout"],
        read_timeout=_CLIENT_OPTIONS["read_timeout"],
    )


def get_shared_container_client(account_name: str, container_name: str, pool_size: int | None = None):
    """Return the process-wide ContainerClient for ``account/container``.

    Thread-safe: the first caller builds the client under a lock; every other
    caller (any thread, any AzureBlobStore instance) gets the same instance.
    Credential resolution is unchanged: FST_PROD_ACCOUNT_KEY / AZURE_STORAGE_KEY,
    then FST_PROD_SAS_TOKEN / AZURE_STORAGE_SAS_TOKEN, then DefaultAzureCredential.
    """
    global _SHARED_DEFAULT_CREDENTIAL
    account_key = os.getenv("FST_PROD_ACCOUNT_KEY") or os.getenv("AZURE_STORAGE_KEY")
    sas_token = os.getenv("FST_PROD_SAS_TOKEN") or os.getenv("AZURE_STORAGE_SAS_TOKEN")
    key = (account_name, container_name, _credential_identity(account_key, sas_token))
    client = _SHARED_CONTAINER_CLIENTS.get(key)
    if client is not None:
        return client
    with _SHARED_CLIENT_LOCK:
        client = _SHARED_CONTAINER_CLIENTS.get(key)
        if client is not None:
            return client
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import ContainerClient
        except ImportError as exc:
            raise RuntimeError(
                "Azure dependencies are missing. Install azure-identity and azure-storage-blob "
                "or run tests with a fake BlobStore."
            ) from exc

        size = max(pool_size or 0, blob_pool_size())
        if account_key:
            credential = account_key
        elif sas_token:
            credential = sas_token
        else:
            if _SHARED_DEFAULT_CREDENTIAL is None:
                _SHARED_DEFAULT_CREDENTIAL = DefaultAzureCredential()
            credential = _SHARED_DEFAULT_CREDENTIAL
        client = ContainerClient(
            f"https://{account_name}.blob.core.windows.net",
            container_name,
            credential=credential,
            transport=_build_pooled_transport(size),
            **_CLIENT_OPTIONS,
        )
        # Non-secret attribute for diagnostics/tests.
        client._backfill_pool_size = size  # type: ignore[attr-defined]
        _SHARED_CONTAINER_CLIENTS[key] = client
        return client


def reset_shared_blob_clients() -> None:
    """Drop cached clients (tests / credential rotation)."""
    global _SHARED_DEFAULT_CREDENTIAL
    with _SHARED_CLIENT_LOCK:
        _SHARED_CONTAINER_CLIENTS.clear()
        _SHARED_DEFAULT_CREDENTIAL = None


@dataclass
class AzureBlobStore:
    account_name: str
    container_name: str
    scan_workers: int | None = None
    _cached_container: object = field(default=None, init=False, repr=False, compare=False)
    _cached_arrow_filesystem: object = field(default=None, init=False, repr=False, compare=False)

    def _container(self):
        if self._cached_container is not None:
            return self._cached_container
        # Reuse the process-wide client so every scanner worker (and every scan
        # request) shares one connection pool sized for BACKFILL_SCAN_WORKERS.
        client = get_shared_container_client(
            self.account_name,
            self.container_name,
            pool_size=blob_pool_size(self.scan_workers),
        )
        self._cached_container = client
        return client

    def read_blob(self, blob_name: str) -> BlobObject:
        try:
            blob = self._container().get_blob_client(blob_name)
            props = blob.get_blob_properties()
            content = blob.download_blob().readall()
        except Exception as exc:
            code = getattr(exc, "error_code", None)
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if code == "BlobNotFound" or status_code == 404:
                raise BlobNotFoundError(blob_name) from exc
            raise

        last_modified = props.last_modified.isoformat() if props.last_modified else None
        return BlobObject(
            name=blob_name,
            content=content,
            url=f"https://{self.account_name}.blob.core.windows.net/{self.container_name}/{blob_name}",
            last_modified=last_modified,
        )

    def read_parquet_metadata(self, blob_name: str) -> BlobObject:
        arrow_path = f"{self.container_name}/{blob_name}"
        try:
            import pyarrow.fs as pafs

            filesystem = self._arrow_filesystem()
            info = filesystem.get_file_info(arrow_path)
            if info.type == pafs.FileType.NotFound:
                raise BlobNotFoundError(blob_name)
            last_modified = info.mtime.isoformat() if info.mtime else None
            return BlobObject(
                name=blob_name,
                content=filesystem.open_input_file(arrow_path),
                url=f"https://{self.account_name}.blob.core.windows.net/{self.container_name}/{blob_name}",
                last_modified=last_modified,
            )
        except BlobNotFoundError:
            raise
        except Exception:
            # Older PyArrow builds and unusual local credential setups may not
            # provide AzureFileSystem. Retain the SDK-backed random-access reader
            # as a correctness-preserving fallback.
            pass

        try:
            blob = self._container().get_blob_client(blob_name)
            props = blob.get_blob_properties()
            size = int(props.size or 0)
        except Exception as exc:
            code = getattr(exc, "error_code", None)
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if code == "BlobNotFound" or status_code == 404:
                raise BlobNotFoundError(blob_name) from exc
            raise

        last_modified = props.last_modified.isoformat() if props.last_modified else None
        return BlobObject(
            name=blob_name,
            content=AzureBlobRangeReader(blob, size),
            url=f"https://{self.account_name}.blob.core.windows.net/{self.container_name}/{blob_name}",
            last_modified=last_modified,
        )

    def _arrow_filesystem(self):
        if self._cached_arrow_filesystem is not None:
            return self._cached_arrow_filesystem

        import pyarrow.fs as pafs

        options = {"account_name": self.account_name}
        account_key = os.getenv("FST_PROD_ACCOUNT_KEY") or os.getenv("AZURE_STORAGE_KEY")
        sas_token = os.getenv("FST_PROD_SAS_TOKEN") or os.getenv("AZURE_STORAGE_SAS_TOKEN")
        if account_key:
            options["account_key"] = account_key
        elif sas_token:
            options["sas_token"] = sas_token
        self._cached_arrow_filesystem = pafs.AzureFileSystem(**options)
        return self._cached_arrow_filesystem

    def list_blob_names(self, prefix: str) -> list[str]:
        try:
            return [blob.name for blob in self._container().list_blobs(name_starts_with=prefix)]
        except Exception:
            raise


@dataclass
class AzureBlobDiscovery:
    """Read-only account/container discovery for the dashboard source picker."""

    default_account: str
    default_container: str

    def list_accounts(self) -> list[str]:
        from .runtime_mode import resolve_runtime

        if resolve_runtime() != "local":
            configured = _explicit_storage_values("MLOPS_DASHBOARD_STORAGE_ACCOUNTS", "FST_PROD_ACCOUNT_NAME")
            return sorted(set(configured))
        accounts = {self.default_account}
        try:
            import subprocess

            result = subprocess.run(
                ["az", "storage", "account", "list", "--query", "[].name", "--output", "json"],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            import json

            accounts.update(item for item in json.loads(result.stdout) if isinstance(item, str))
        except Exception:
            # The configured source remains usable even if Azure CLI account discovery is unavailable.
            pass
        return sorted(accounts)

    def list_containers(self, account_name: str) -> list[str]:
        account_name = _validate_storage_name(account_name, field="account")
        from .runtime_mode import resolve_runtime

        if resolve_runtime() != "local":
            configured_accounts = set(_explicit_storage_values("MLOPS_DASHBOARD_STORAGE_ACCOUNTS", "FST_PROD_ACCOUNT_NAME"))
            if account_name not in configured_accounts:
                return []
            configured = _explicit_storage_values("MLOPS_DASHBOARD_STORAGE_CONTAINERS", "FST_PROD_CONTAINER")
            return sorted(set(configured))
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient

            client = BlobServiceClient(
                account_url=f"https://{account_name}.blob.core.windows.net",
                credential=DefaultAzureCredential(),
            )
            containers = [container.name for container in client.list_containers()]
        except Exception as exc:
            raise RuntimeError(f"Could not list containers for {account_name}: {exc}") from exc

        if account_name == self.default_account:
            containers.append(self.default_container)
        return sorted(set(containers))


def _explicit_storage_values(*names: str) -> list[str]:
    """Return only values deliberately supplied to the deployed app."""
    values: list[str] = []
    for name in names:
        raw = os.getenv(name, "")
        values.extend(item.strip().lower() for item in raw.split(",") if item.strip())
    return values


def _validate_storage_name(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9-]{3,63}", normalized):
        raise ValueError(f"Invalid Azure storage {field}")
    return normalized
