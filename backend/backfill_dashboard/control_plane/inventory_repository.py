from __future__ import annotations

import json
from typing import Iterable, Sequence

from ..inventory_models import MachineFacets, MachinePage, MachineRecord, MachineSearchQuery
from .database import Database
from .inventory_sql import FIELDS, decode_seek, encode_seek, query_where, record_values, row_record, sort_spec


class InventoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def begin_sync(self) -> None:
        with self.database.connect() as db:
            db.execute("DELETE FROM inventory_stage")

    def stage(self, records: Iterable[MachineRecord]) -> int:
        rows = [record_values(record) for record in records]
        if not rows:
            return 0
        marks = ",".join("?" for _ in FIELDS)
        with self.database.connect() as db:
            db.execute("BEGIN")
            db.executemany(f"INSERT INTO inventory_stage({','.join(FIELDS)}) VALUES({marks})", rows)
            db.execute("COMMIT")
        return len(rows)

    def finish_sync(self, version: str) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM inventory")
            db.execute(f"INSERT INTO inventory({','.join(FIELDS)}) SELECT {','.join(FIELDS)} FROM inventory_stage")
            db.execute("DELETE FROM inventory_stage")
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('inventory_version',?)", (version,))
            db.execute("COMMIT")

    def get_version(self) -> str:
        return self.database.metadata("inventory_version", "unavailable")

    def search(self, query: MachineSearchQuery) -> MachinePage:
        where, values = query_where(query)
        sort_field, direction = sort_spec(query)
        cursor = decode_seek(query.cursor)
        if cursor:
            operator = ">" if direction == "ASC" else "<"
            where.append(f"(COALESCE({sort_field},'') {operator} ? OR (COALESCE({sort_field},'')=? AND machine_id {operator} ?))")
            values.extend((cursor[0], cursor[0], cursor[1]))
        clause = " WHERE " + " AND ".join(where) if where else ""
        limit = min(max(query.limit, 1), 200)
        with self.database.connect() as db:
            rows = db.execute(f"SELECT * FROM inventory{clause} ORDER BY COALESCE({sort_field},'') {direction}, machine_id {direction} LIMIT ?", (*values, limit + 1)).fetchall()
            total_clause, total_values = query_where(query)
            total_sql = " WHERE " + " AND ".join(total_clause) if total_clause else ""
            total = db.execute(f"SELECT COUNT(*) FROM inventory{total_sql}", total_values).fetchone()[0]
        page = rows[:limit]
        next_cursor = encode_seek(str(page[-1][sort_field] or ""), str(page[-1]["machine_id"])) if len(rows) > limit else None
        return MachinePage([row_record(row) for row in page], next_cursor, self.get_version(), total)

    def get_many(self, machine_ids: Sequence[str]) -> list[MachineRecord]:
        result: list[MachineRecord] = []
        with self.database.connect() as db:
            db.execute("BEGIN")
            for offset in range(0, len(machine_ids), 500):
                chunk = list(machine_ids[offset:offset + 500])
                if not chunk:
                    continue
                marks = ",".join("?" for _ in chunk)
                result.extend(row_record(row) for row in db.execute(f"SELECT * FROM inventory WHERE machine_id IN ({marks})", chunk))
            db.execute("COMMIT")
        return result

    def get_facets(self, query: MachineSearchQuery) -> MachineFacets:
        where, values = query_where(query)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.database.connect() as db:
            def counts(field: str) -> dict[str, int]:
                return {str(row[0]).lower(): int(row[1]) for row in db.execute(f"SELECT {field},COUNT(*) FROM inventory{clause} GROUP BY {field}", values) if row[0] not in (None, "")}
            issues: dict[str, int] = {}
            for row in db.execute(f"SELECT classification_reason,COUNT(*) FROM inventory{clause} GROUP BY classification_reason", values):
                for reason in json.loads(row[0]):
                    issues[reason] = issues.get(reason, 0) + int(row[1])
            return MachineFacets(counts("resource_cohort"), counts("status"), counts("backfill_eligible"),
                                 counts("site_name"), counts("organization_name"), issues)
