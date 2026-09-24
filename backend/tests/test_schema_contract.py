from __future__ import annotations

import json

import pytest

from backfill_dashboard.config import Settings
from backfill_dashboard.schema_contract import load_fst_schema_contract


def test_bundled_fst_schema_contract_is_versioned_and_complete() -> None:
    contract = load_fst_schema_contract(Settings().canonical_schema_path)

    assert contract.schema_version == "fst-features-2026-09-15-v1"
    assert len(contract.columns) == 1051
    assert len(set(contract.columns)) == 1051
    assert "ultrasonic_p2p_v2" in contract.columns
    assert "machine_non_stationary_score" in contract.columns
    assert "machine_id" not in contract.columns
    assert "quarter" not in contract.columns
    assert "month" not in contract.columns


def test_schema_contract_rejects_duplicate_columns(tmp_path) -> None:
    path = tmp_path / "schema.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "test-v1",
                "source": {},
                "columns": ["feature", "feature"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="duplicate columns"):
        load_fst_schema_contract(path)
