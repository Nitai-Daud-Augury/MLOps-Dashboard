from __future__ import annotations

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


@dataclass
class AzureBlobStore:
    account_name: str
    container_name: str
    _cached_container: object = field(default=None, init=False, repr=False, compare=False)
    _cached_arrow_filesystem: object = field(default=None, init=False, repr=False, compare=False)

    def _container(self):
        if self._cached_container is not None:
            return self._cached_container

        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import ContainerClient
        except ImportError as exc:
            raise RuntimeError(
                "Azure dependencies are missing. Install azure-identity and azure-storage-blob "
                "or run tests with a fake BlobStore."
            ) from exc

        account_url = f"https://{self.account_name}.blob.core.windows.net"
        account_key = os.getenv("FST_PROD_ACCOUNT_KEY") or os.getenv("AZURE_STORAGE_KEY")
        sas_token = os.getenv("FST_PROD_SAS_TOKEN") or os.getenv("AZURE_STORAGE_SAS_TOKEN")
        client_options = {
            "connection_timeout": 10,
            "read_timeout": 60,
            "retry_total": 2,
            "retry_backoff_factor": 0.5,
        }
        if account_key:
            client = ContainerClient(account_url, self.container_name, credential=account_key, **client_options)
        elif sas_token:
            client = ContainerClient(account_url, self.container_name, credential=sas_token, **client_options)
        else:
            client = ContainerClient(
                account_url,
                self.container_name,
                credential=DefaultAzureCredential(),
                **client_options,
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
