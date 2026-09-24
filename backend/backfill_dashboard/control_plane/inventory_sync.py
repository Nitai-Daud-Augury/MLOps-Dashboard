from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

from ..inventory_models import MachineSearchQuery
from .database import Database
from .inventory_repository import InventoryRepository


class InventorySynchronizer:
    def __init__(self, source, repository: InventoryRepository, database: Database, source_status: str) -> None:
        self.source = source
        self.repository = repository
        self.database = database
        self.source_status = source_status
        self.source_kind = "mongodb" if source_status == "healthy" else "file_fallback" if source_status == "not_configured" else "unavailable"
        self._lock = threading.Lock()

    def status(self) -> dict:
        return {
            "status": self.database.metadata("inventory_status", self.source_status),
            "inventory_version": self.repository.get_version(),
            "last_sync_at": self.database.metadata("inventory_last_sync"),
            "last_error": self.database.metadata("inventory_error") or None,
            "source": self.source_kind,
        }

    def sync(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {**self.status(), "refreshing": True}
        count = 0
        try:
            self.database.set_metadata("inventory_status", "refreshing")
            self.repository.begin_sync()
            sync_version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            cursor = None
            batch_size = min(1000, max(100, int(os.getenv("BACKFILL_MONGO_SYNC_BATCH_SIZE", "500"))))
            while True:
                page = self._source_page(cursor, batch_size)
                count += self.repository.stage(replace(record, inventory_version=sync_version) for record in page.machines)
                cursor = page.next_cursor
                if not cursor:
                    break
            self.repository.finish_sync(sync_version)
            self.database.set_metadata("inventory_status", "healthy")
            self.database.set_metadata("inventory_last_sync", datetime.now(timezone.utc).isoformat())
            self.database.set_metadata("inventory_error", "")
            return {**self.status(), "machine_count": count}
        except Exception as exc:
            self.database.set_metadata("inventory_status", self.source_status if self.source_status != "healthy" else "unreachable")
            self.database.set_metadata("inventory_error", _redacted(exc))
            return {**self.status(), "machine_count": count}
        finally:
            self._lock.release()

    def _source_page(self, cursor: str | None, batch_size: int):
        attempts = max(1, min(5, int(os.getenv("BACKFILL_MONGO_READ_ATTEMPTS", "3"))))
        for attempt in range(1, attempts + 1):
            try:
                return self.source.search(MachineSearchQuery(cursor=cursor, limit=batch_size))
            except Exception:
                if attempt == attempts:
                    raise
                delay = min(8.0, 2 ** (attempt - 1))
                time.sleep(delay + random.uniform(0, delay * .25))


def _redacted(exc: Exception) -> str:
    text = str(exc)
    for marker in ("mongodb://", "mongodb+srv://"):
        if marker in text:
            return "MongoDB dependency error (connection details redacted)"
    return text[:500]
