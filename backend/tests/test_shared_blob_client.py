"""Process-wide shared Azure blob client: one pool for all FST scan workers.

These tests never touch the network: ContainerClient construction is lazy and
``list_blobs`` is stubbed. A fake (non-secret) account key is injected so the
developer's real credentials from .env are never used or printed.
"""
from __future__ import annotations

import base64
import threading
from dataclasses import replace

import pytest

from backfill_dashboard import storage
from backfill_dashboard.config import Settings
from backfill_dashboard.scanner import BackfillScanner
from backfill_dashboard.storage import (
    AzureBlobStore,
    blob_pool_size,
    get_shared_container_client,
    reset_shared_blob_clients,
)

FAKE_KEY = base64.b64encode(b"not-a-real-storage-key").decode()


def _adapter(client):
    session = client._pipeline._transport.session
    return session.get_adapter("https://unit-test-acct.blob.core.windows.net/")


@pytest.fixture(autouse=True)
def _isolated_shared_clients(monkeypatch):
    monkeypatch.setenv("FST_PROD_ACCOUNT_KEY", FAKE_KEY)
    for name in ("AZURE_STORAGE_KEY", "FST_PROD_SAS_TOKEN", "AZURE_STORAGE_SAS_TOKEN", "BACKFILL_BLOB_POOL_SIZE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BACKFILL_SCAN_WORKERS", "12")
    reset_shared_blob_clients()
    yield
    reset_shared_blob_clients()


def test_singleton_returns_same_instance_across_threads():
    barrier = threading.Barrier(16)
    results: list[object] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        client = get_shared_container_client("unit-test-acct", "unit-test-container")
        with lock:
            results.append(client)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 16
    assert len({id(client) for client in results}) == 1
    assert len(storage._SHARED_CONTAINER_CLIENTS) == 1


def test_cache_key_never_contains_the_raw_credential():
    get_shared_container_client("unit-test-acct", "unit-test-container")
    assert FAKE_KEY not in repr(list(storage._SHARED_CONTAINER_CLIENTS))


def test_different_container_or_credential_gets_its_own_client(monkeypatch):
    first = get_shared_container_client("unit-test-acct", "unit-test-container")
    other_container = get_shared_container_client("unit-test-acct", "other-container")
    monkeypatch.setenv("FST_PROD_ACCOUNT_KEY", base64.b64encode(b"rotated-fake-key").decode())
    rotated = get_shared_container_client("unit-test-acct", "unit-test-container")

    assert first is not other_container
    assert first is not rotated


def test_pool_size_derives_from_worker_count_with_headroom(monkeypatch):
    assert blob_pool_size(12) == 16
    assert blob_pool_size(1) == 10  # floor: never below the old urllib3 default
    monkeypatch.setenv("BACKFILL_SCAN_WORKERS", "30")
    assert blob_pool_size() == 34
    # Override can raise the pool but never shrink it below workers.
    monkeypatch.setenv("BACKFILL_BLOB_POOL_SIZE", "4")
    assert blob_pool_size(30) == 34
    monkeypatch.setenv("BACKFILL_BLOB_POOL_SIZE", "64")
    assert blob_pool_size(30) == 64


@pytest.mark.parametrize("workers", [1, 12, 24, 50])
def test_shared_client_http_pool_is_at_least_scan_workers(monkeypatch, workers):
    monkeypatch.setenv("BACKFILL_SCAN_WORKERS", str(workers))
    client = get_shared_container_client("unit-test-acct", "unit-test-container")
    adapter = _adapter(client)

    assert adapter._pool_maxsize >= workers
    assert adapter._pool_connections >= workers
    assert adapter._pool_maxsize >= 10  # never below the old urllib3 default


def test_shared_transport_is_not_owned_and_blob_clients_reuse_it():
    client = get_shared_container_client("unit-test-acct", "unit-test-container")
    transport = client._pipeline._transport
    blob_client = client.get_blob_client("machine_id=x/part-0.parquet")

    assert transport._session_owner is False
    assert blob_client._pipeline._transport._transport is transport
    assert transport.connection_config.timeout == 10
    assert transport.connection_config.read_timeout == 60


def test_store_instances_share_one_client_sized_for_their_workers():
    # The API builds a new AzureBlobStore per scan request and per feature-series
    # request; they must all land on the same pooled client.
    first = AzureBlobStore("unit-test-acct", "unit-test-container", scan_workers=20)
    second = AzureBlobStore("unit-test-acct", "unit-test-container", scan_workers=20)

    assert first._container() is second._container()
    assert _adapter(first._container())._pool_maxsize >= 20


def test_scanner_workers_use_the_shared_client(monkeypatch):
    seen_clients: set[int] = set()
    seen_threads: set[int] = set()
    lock = threading.Lock()
    from azure.storage.blob import ContainerClient

    def fake_list_blobs(self, name_starts_with=None, **kwargs):
        with lock:
            seen_clients.add(id(self))
            seen_threads.add(threading.get_ident())
        return []

    monkeypatch.setattr(ContainerClient, "list_blobs", fake_list_blobs)
    settings = replace(Settings(), scan_workers=8)
    scanner = BackfillScanner(
        settings=settings,
        inventory=None,
        blob_store=AzureBlobStore("unit-test-acct", "unit-test-container", scan_workers=settings.scan_workers),
        silver_provider=None,
    )

    scanner._discover_coverage_starts([f"machine-{index}" for index in range(40)])

    shared = get_shared_container_client("unit-test-acct", "unit-test-container")
    assert seen_clients == {id(shared)}
    assert len(storage._SHARED_CONTAINER_CLIENTS) == 1
    assert _adapter(shared)._pool_maxsize >= settings.scan_workers
