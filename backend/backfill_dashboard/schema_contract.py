from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FstSchemaContract:
    schema_version: str
    columns: tuple[str, ...]
    source: dict[str, object]


def load_fst_schema_contract(path: Path) -> FstSchemaContract:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not load canonical FST schema from {path}: {exc}") from exc

    version = payload.get("schema_version")
    columns = payload.get("columns")
    source = payload.get("source", {})
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError(f"Canonical FST schema at {path} has no schema_version")
    if not isinstance(columns, list) or not columns or not all(
        isinstance(column, str) and column for column in columns
    ):
        raise RuntimeError(f"Canonical FST schema at {path} has invalid columns")
    if len(columns) != len(set(columns)):
        raise RuntimeError(f"Canonical FST schema at {path} contains duplicate columns")
    if not isinstance(source, dict):
        raise RuntimeError(f"Canonical FST schema at {path} has invalid source metadata")

    return FstSchemaContract(version, tuple(columns), source)
