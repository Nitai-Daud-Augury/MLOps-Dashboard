from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backfill_dashboard.lifecycle_databricks import (
    DatabricksLifecycleProvider,
    _normalize_machines_raw_row,
    build_databricks_lifecycle_provider,
)
from backfill_dashboard.inventory_provider import _is_test_machine, _record
from backfill_dashboard.lifecycle import build_lifecycle_provider
from sibling_repos import requires_canonical_classifier


def _row(**overrides):
    base = {
        "machine_id": "m1",
        "machine_name": "Pump ULRPM",
        "company": "Organization",
        "status": "active",
        "baseline_status": "OK",
        "detailed_status": "ONLINE",
        "status_changed_at": "2026-06-15T01:00:00Z",
        "first_recorded": "2024-07-01T00:00:00Z",
        "last_recorded": "2026-06-15T00:00:00Z",
        "created_at": "2024-08-14T12:00:00Z",
        "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "tags_json": '["ulrpm"]',
        "contained_in_json": (
            '{"_id":"site1","name":"Plant","type":"facility",'
            '"company":{"_id":"org1","name":"Organization"}}'
        ),
    }
    base.update(overrides)
    return base


def test_normalize_maps_bronze_fields_and_prefers_created_at():
    document = _normalize_machines_raw_row(_row())
    assert document["machine_id"] == "m1"
    assert document["name"] == "Pump ULRPM"
    assert document["tags"] == ["ulrpm"]
    assert document["status"] == "active"
    assert document["siteId"] == "site1"
    assert document["organizationId"] == "org1"
    assert document["last_recorded_at"] == "2026-06-15T00:00:00Z"
    assert document["installation_date"] == "2024-08-14T12:00:00Z"


def test_installation_falls_back_to_first_recorded_when_created_at_missing():
    document = _normalize_machines_raw_row(_row(created_at=None))
    assert document["installation_date"] == "2024-07-01T00:00:00Z"


def test_status_falls_back_to_detailed_status():
    document = _normalize_machines_raw_row(_row(status="", detailed_status="ONLINE"))
    assert document["status"] == "ONLINE"


def test_e2e_name_marks_test_via_record_heuristic():
    document = _normalize_machines_raw_row(
        _row(
            machine_id="69525fce633df3bd4016c788",
            machine_name="ULRPM E2E test #3",
            company="Some Plant Co",
        )
    )
    record = _record(document, "test")
    assert record.display_name == "ULRPM E2E test #3"
    assert record.is_test_machine is True


def test_company_demo_marks_test():
    assert _is_test_machine("Production Pump", [], "Demo") is True
    assert _is_test_machine("Production Pump", [], "dynamicsScopingTest") is True
    assert _is_test_machine("Production Pump", [], "QA") is True
    assert _is_test_machine("Production Pump", [], "Hagay test lab") is True
    assert _is_test_machine("Production Pump", [], "Acme") is False


@requires_canonical_classifier
def test_provider_get_many_maps_rows_through_record():
    columns = list(_row().keys())
    values = tuple(_row().values())

    def connect(query: str):
        assert "raw_json:_id::string IN" in query
        assert "'m1'" in query
        assert "QUALIFY ROW_NUMBER()" in query
        assert "ingestion_timestamp DESC" in query
        assert "machines_raw" in query or "FROM" in query
        return columns, [values]

    provider = DatabricksLifecycleProvider(
        table="dih_prod.bronze_augury_mh_mongodb.machines_raw",
        profile="mlops-dev",
        warehouse_id="6ed9ddd0b2661edc",
        connect=connect,
    )
    record = provider.get_many(["m1"])[0]
    assert record.machine_id == "m1"
    assert record.display_name == "Pump ULRPM"
    assert record.resource_cohort == "ulrpm"
    assert record.installation_at == "2024-08-14T12:00:00Z"
    assert record.last_recorded_at == "2026-06-15T00:00:00Z"
    assert record.site_id == "site1"
    assert record.organization_id == "org1"
    # is_test_machine stays heuristic/file-registry driven; ordinary name is not test.
    assert not record.is_test_machine


def test_provider_get_many_empty_ids_short_circuits():
    def connect(query: str):  # pragma: no cover - must not run
        raise AssertionError("connect should not run for empty ids")

    provider = DatabricksLifecycleProvider(
        table="dih_prod.bronze_augury_mh_mongodb.machines_raw",
        profile="mlops-dev",
        warehouse_id="wh",
        connect=connect,
    )
    assert provider.get_many([]) == []


def test_provider_sql_error_propagates_for_scanner_fail_open():
    def connect(query: str):
        raise RuntimeError("warehouse unavailable")

    provider = DatabricksLifecycleProvider(
        table="dih_prod.bronze_augury_mh_mongodb.machines_raw",
        profile="mlops-dev",
        warehouse_id="wh",
        connect=connect,
    )
    with pytest.raises(RuntimeError, match="warehouse unavailable"):
        provider.get_many(["m1"])


def test_build_databricks_requires_warehouse(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID", raising=False)
    provider, status = build_databricks_lifecycle_provider()
    assert provider is None and status == "not_configured"


def test_build_prefers_machines_raw_table_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "6ed9ddd0b2661edc")
    monkeypatch.setenv("DATABRICKS_PROFILE", "mlops-dev")
    monkeypatch.setenv(
        "DATABRICKS_MACHINES_RAW_TABLE",
        "dih_prod.bronze_augury_mh_mongodb.machines_raw",
    )
    monkeypatch.setenv("DATABRICKS_EQUIPMENT_TABLE", "dih_prod.silver_ontology.equipment")

    class FakeConfig:
        host = "https://example.databricks.com"

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(
        "backfill_dashboard.lifecycle_databricks._create_config",
        lambda config_class, profile: FakeConfig(),
    )
    provider, status = build_databricks_lifecycle_provider()
    assert status == "healthy"
    assert provider is not None
    assert provider.table == "dih_prod.bronze_augury_mh_mongodb.machines_raw"


def test_factory_selects_databricks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFECYCLE_SOURCE", "databricks")
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "6ed9ddd0b2661edc")
    monkeypatch.setenv("DATABRICKS_PROFILE", "mlops-dev")

    class FakeConfig:
        host = "https://example.databricks.com"

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(
        "backfill_dashboard.lifecycle_databricks._create_config",
        lambda config_class, profile: FakeConfig(),
    )
    provider, status = build_lifecycle_provider()
    assert status == "healthy"
    assert isinstance(provider, DatabricksLifecycleProvider)


def test_factory_openapi_and_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFECYCLE_SOURCE", "openapi")
    assert build_lifecycle_provider() == (None, "not_configured")
    monkeypatch.setenv("LIFECYCLE_SOURCE", "off")
    assert build_lifecycle_provider() == (None, "off")


def test_factory_defaults_to_mongo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIFECYCLE_SOURCE", raising=False)
    monkeypatch.delenv("MONGODB_URL", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.setenv("MONGODB_KEY_VAULT_ENABLED", "0")
    provider, status = build_lifecycle_provider()
    assert provider is None
    assert status == "not_configured"

def test_execute_timeout_raises_for_scanner_fail_open(monkeypatch: pytest.MonkeyPatch):
    import backfill_dashboard.lifecycle_databricks as mod
    import time

    def hang(query, *, timeout_seconds=None):
        time.sleep(5)
        return ["machine_id"], [("m1",)]

    monkeypatch.setattr(mod, "_timeout_seconds", lambda: 0.05)
    provider = DatabricksLifecycleProvider(
        table="dih_prod.bronze_augury_mh_mongodb.machines_raw",
        profile="mlops-dev",
        warehouse_id="wh",
        timeout_seconds=0.05,
        cold_timeout_seconds=0.05,
    )
    monkeypatch.setattr(provider, "_execute_databricks", hang)
    with pytest.raises(TimeoutError, match="timed out"):
        provider.get_many(["m1"])


def test_first_sql_uses_cold_timeout_then_marks_warm(monkeypatch: pytest.MonkeyPatch):
    budgets: list[float] = []

    def fake_execute(query, *, timeout_seconds=None):
        budgets.append(float(timeout_seconds))
        return ["machine_id", "machine_name"], [("m1", "Pump")]

    provider = DatabricksLifecycleProvider(
        table="dih_prod.bronze_augury_mh_mongodb.machines_raw",
        profile="mlops-dev",
        warehouse_id="wh",
        timeout_seconds=45.0,
        cold_timeout_seconds=180.0,
    )
    monkeypatch.setattr(provider, "_execute_databricks", fake_execute)
    # Bypass connect injection so _execute takes the timeout path.
    provider._connect = None
    assert provider._sql_warm is False
    rows = provider._execute("SELECT 1")
    assert rows[0] == ["machine_id", "machine_name"]
    assert budgets == [180.0]
    assert provider._sql_warm is True
    provider._execute("SELECT 2")
    assert budgets == [180.0, 45.0]


def test_warm_timeout_retries_once_with_cold_budget(monkeypatch: pytest.MonkeyPatch):
    import time

    calls: list[float] = []

    def flaky(query, *, timeout_seconds=None):
        budget = float(timeout_seconds)
        calls.append(budget)
        if len(calls) == 1:
            time.sleep(0.2)
            return ["machine_id"], [("m1",)]
        return ["machine_id", "machine_name"], [("m1", "Pump")]

    provider = DatabricksLifecycleProvider(
        table="dih_prod.bronze_augury_mh_mongodb.machines_raw",
        profile="mlops-dev",
        warehouse_id="wh",
        timeout_seconds=0.05,
        cold_timeout_seconds=1.0,
    )
    provider._sql_warm = True
    provider._connect = None
    monkeypatch.setattr(provider, "_execute_databricks", flaky)
    columns, rows = provider._execute("SELECT 1")
    assert calls[0] == 0.05
    assert calls[1] == 1.0
    assert columns == ["machine_id", "machine_name"]
    assert rows == [("m1", "Pump")]


def test_build_timeout_returns_unreachable(monkeypatch: pytest.MonkeyPatch):
    import backfill_dashboard.lifecycle_databricks as mod
    import time

    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "6ed9ddd0b2661edc")
    monkeypatch.setenv("DATABRICKS_PROFILE", "mlops-dev")
    monkeypatch.setenv("LIFECYCLE_DATABRICKS_TIMEOUT_SECONDS", "0.05")

    def hang_config(config_class, profile):
        time.sleep(5)
        return type("Cfg", (), {"host": "https://example.databricks.com"})()

    monkeypatch.setattr(mod, "_create_config", hang_config)
    # Ensure import path thinks SDK is present
    provider, status = build_databricks_lifecycle_provider()
    assert provider is None and status == "unreachable"

