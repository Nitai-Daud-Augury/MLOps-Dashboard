from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schema import SCHEMA


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(campaigns)")}
            if "production" not in columns:
                connection.execute("ALTER TABLE campaigns ADD COLUMN production INTEGER NOT NULL DEFAULT 0")
            inventory_columns = {row[1] for row in connection.execute("PRAGMA table_info(inventory)")}
            if "last_recorded_at" not in inventory_columns:
                connection.execute("ALTER TABLE inventory ADD COLUMN last_recorded_at TEXT")
                connection.execute("ALTER TABLE inventory_stage ADD COLUMN last_recorded_at TEXT")
            if "installation_at" not in inventory_columns:
                connection.execute("ALTER TABLE inventory ADD COLUMN installation_at TEXT")
                connection.execute("ALTER TABLE inventory_stage ADD COLUMN installation_at TEXT")
            # Keep both live and staging tables aligned with MachineRecord. The
            # staging table predates migrations and must be upgraded separately.
            for table in ("inventory", "inventory_stage"):
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if "is_test_machine" not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN is_test_machine INTEGER NOT NULL DEFAULT 0"
                    )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def metadata(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
