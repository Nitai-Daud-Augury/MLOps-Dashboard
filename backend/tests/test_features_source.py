"""FEATURES_SOURCE=blob|databricks: the runtime switch, result tagging and the Databricks reader.

Every Databricks call is a fake executor; nothing here touches a warehouse or blob.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest

import backfill_dashboard.app as app_module
import backfill_dashboard.scanner as scanner_module
from backfill_dashboard.config import Settings
from backfill_dashboard.control_store import JsonControlStore
from backfill_dashboard.feature_store_source import FeatureStoreSource, build_coverage_query
from backfill_dashboard.features_source import (
    FeaturesSourceSetting,
    SCHEMA_UNAVAILABLE_NOTE,
    env_default,
    results_status,
)
from backfill_dashboard.models import MonthPartition, ScanState
from backfill_dashboard.months import orchestrator_month_index
from backfill_dashboard.parquet_inspector import ParquetFeatureSummary
from backfill_dashboard.repository import ReportRepository
from backfill_dashboard.storage import BlobNotFoundError, BlobObject

MID = "68b0667a7e20e7c69b3c7350"
V2 = "ultrasonic_rms_v2"
V1 = "ultrasonic_rms"
CALENDAR = [(2026, m) for m in range(1, 6)]
TABLE = "dih_prod.silver_mh.feature_store"
BRONZE_PREFIX = "/Volumes/dih_prod/bronze_augury_mh_blob/feature-store-production/"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("FEATURES_SOURCE", "FEATURES_CROSSCHECK", "FEATURES_SOURCE_TIMEOUT_SECONDS", "DATABRICKS_FEATURE_STORE_TABLE"):
        monkeypatch.delenv(key, raising=False)


def _path(y, m):
    return f"machine_id={MID}/quarter={y}Q{(m - 1) // 3 + 1}/month={m}/partition_version=last/part-0.parquet"


# (rows, v2 non-null, rows with v1 set but v2 null)
FS = {(2026, 1): (1000, 1000, 0), (2026, 2): (500, 0, 0), (2026, 3): (800, 790, 10), (2026, 5): (100, 100, 0)}


class FakeSql:
    """Answers the three statements FeatureStoreSource issues."""

    def __init__(self, data=None, table_columns=("machine_id", "recorded_at", V1, V2), delay=0.0, error=None):
        self.data = FS if data is None else data
        self.table_columns = list(table_columns)
        self.delay, self.error = delay, error
        self.queries: list[str] = []

    def __call__(self, query):
        self.queries.append(query)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        if query.endswith("LIMIT 0"):
            return self.table_columns, []
        if "_source_file" in query:
            return ["_source_file", "bronze_modified"], [
                (BRONZE_PREFIX + _path(y, m), f"2026-0{m}-28 10:00:00") for (y, m) in self.data
            ]
        columns = ["machine_id", "year", "month", "row_count", f"nn__{V2}", f"gap__{V2}", f"gapmin__{V2}", f"gapmax__{V2}"]
        rows = [
            (MID.upper() if m == 1 else MID, y, m, rows, nn, gap,
             f"{y}-{m:02d}-03 00:00:00" if gap else None, f"{y}-{m:02d}-09 00:00:00" if gap else None)
            for (y, m), (rows, nn, gap) in self.data.items()
        ]
        return columns, rows


class Store:
    """Blob store: the blob scan reads these; databricks mode must never call it."""

    def __init__(self, forbid=False):
        self.forbid = forbid
        self.calls = 0

    def list_blob_names(self, prefix):
        self.calls += 1
        if self.forbid:
            raise AssertionError("databricks mode must not list blob")
        return [_path(y, m) for (y, m) in FS if _path(y, m).startswith(prefix)]

    def read_parquet_metadata(self, name):
        self.calls += 1
        if self.forbid:
            raise AssertionError("databricks mode must not read blob")
        for (y, m) in FS:
            if name == _path(y, m):
                return BlobObject(name, (y, m), "https://example.test/" + name, "2026-09-28T12:13:08+00:00")
        raise BlobNotFoundError(name)

    read_blob = read_parquet_metadata


def _blob_summary(content, features):
    rows, nn, gap = FS[content]
    gaps = {V2: {"missing_rows": gap}} if gap else {}
    return ParquetFeatureSummary(rows, ["machine_id", V1, V2], {V2: nn}, gaps)


def _scan(monkeypatch, source="blob", sql=None, store=None, timeout=None):
    settings = Settings(scan_workers=2)
    object.__setattr__(settings, "target_features", [V2])
    object.__setattr__(settings, "test_machine_ids", ())
    inventory = SimpleNamespace(list_machine_ids=lambda: [MID])
    store = store or Store(forbid=source == "databricks")
    scanner = scanner_module.BackfillScanner(settings, inventory, store, SimpleNamespace(row_counts=lambda *_: {}), None, "off")
    scanner.features_source = source
    if sql is not None:
        scanner.feature_store_source = FeatureStoreSource(table=TABLE, executor=sql, timeout_seconds=timeout or 30)
    monkeypatch.setattr(
        scanner, "_coverage_partitions",
        lambda starts: [MonthPartition(orchestrator_month_index(y, m), y, m) for y, m in CALENDAR],
    )
    # The canonical contract has a column neither side carries: blob flags it, databricks must not.
    monkeypatch.setattr(scanner_module, "load_fst_schema_contract",
                        lambda _: SimpleNamespace(columns=("machine_id", V1, V2, "absent_in_dbx"), schema_version="test"))
    monkeypatch.setattr(scanner_module, "inspect_feature_partition", _blob_summary)
    return scanner.scan("scan-1"), store


def _months(snapshot):
    return {(mo.partition.year, mo.partition.month): mo for mo in snapshot.machines[0].months}


class Client:
    """Minimal ASGI client (the repo has no httpx test dependency)."""

    def __init__(self, app):
        self.app = app

    def request(self, method, path, json_body=None, headers=None):
        async def run():
            sent, received = [], False
            body = json.dumps(json_body or {}).encode()

            async def receive():
                nonlocal received
                if received:
                    return {"type": "http.disconnect"}
                received = True
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                sent.append(message)

            raw_headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
            raw_headers += [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
            await self.app({
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                "root_path": "", "headers": raw_headers, "client": ("testclient", 50000), "server": ("testserver", 80),
            }, receive, send)
            start = next(item for item in sent if item["type"] == "http.response.start")
            content = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
            return SimpleNamespace(status_code=start["status"], json=lambda: json.loads(content))

        return asyncio.run(run())

    def get(self, path, headers=None):
        return self.request("GET", path, headers=headers)

    def put(self, path, json=None, headers=None):
        return self.request("PUT", path, json, headers)

    def post(self, path, json=None, headers=None):
        return self.request("POST", path, json, headers)


# --- env default + persistence ---------------------------------------------------

@pytest.mark.parametrize("raw, expected, error", [
    (None, "blob", False), ("", "blob", False), ("blob", "blob", False),
    ("Databricks", "databricks", False), ("s3", "blob", True),
])
def test_env_default(raw, expected, error):
    environ = {} if raw is None else {"FEATURES_SOURCE": raw}
    source, config_error = env_default(environ)
    assert source == expected and bool(config_error) is error


def test_switch_is_persisted_in_the_control_store_and_reloaded(tmp_path, caplog):
    store = JsonControlStore(tmp_path / "control.json")
    setting = FeaturesSourceSetting(store, fallback_path=tmp_path / "fallback.json", environ={}, load_in_background=False)
    assert setting.describe()["source"] == "blob" and setting.describe()["origin"] == "env_default"
    with caplog.at_level(logging.INFO, logger="uvicorn.error.data_source"):
        described = setting.switch("databricks", actor="nitai@augury.com")
    assert described["source"] == "databricks" and described["persistence"]["durable"] is True
    assert "[data-source] features source switched: blob -> databricks by nitai@augury.com" in caplog.text
    # A new process (e.g. after an Apps restart) reads it back from the control store.
    reloaded = FeaturesSourceSetting(store, environ={"FEATURES_SOURCE": "blob"}, load_in_background=False)
    assert reloaded.current() == "databricks" and reloaded.describe()["origin"] == "persisted"
    assert reloaded.describe()["updated_by"] == "nitai@augury.com" and reloaded.describe()["previous"] == "blob"
    # Settings never show up as month records.
    assert store.list_machine_states(MID) == []
    assert not (tmp_path / "fallback.json").exists()


def test_env_default_applies_when_nothing_is_persisted(tmp_path):
    store = JsonControlStore(tmp_path / "control.json")
    setting = FeaturesSourceSetting(store, environ={"FEATURES_SOURCE": "databricks"}, load_in_background=False)
    assert setting.current() == "databricks" and setting.describe()["persistence"]["load_state"] == "not_found"


class BrokenStore:
    def describe_settings_location(self, name):
        return f"azure_blob:test/{name}"

    def get_setting(self, name):
        raise RuntimeError("Manifest upload is unavailable: no credential")

    put_setting = lambda self, name, record: self.get_setting(name)  # noqa: E731


def test_unwritable_control_store_keeps_the_switch_and_says_it_is_not_durable(tmp_path):
    fallback = tmp_path / "fallback.json"
    setting = FeaturesSourceSetting(BrokenStore(), fallback_path=fallback, environ={}, load_in_background=False)
    described = setting.switch("databricks", actor="a")
    assert described["source"] == "databricks"
    assert described["persistence"]["durable"] is False and "no credential" in described["persistence"]["error"]
    assert json.loads(fallback.read_text())["features_source"]["value"] == "databricks"
    again = FeaturesSourceSetting(BrokenStore(), fallback_path=fallback, environ={}, load_in_background=False)
    assert again.current() == "databricks" and again.describe()["persistence"]["durable"] is False
    with pytest.raises(ValueError):
        setting.switch("s3")


def test_background_load_never_blocks_and_env_default_applies_meanwhile(tmp_path):
    class SlowStore(BrokenStore):
        def get_setting(self, name):
            time.sleep(0.3)
            return {"value": "databricks"}

    setting = FeaturesSourceSetting(SlowStore(), environ={}, load_in_background=True)
    started = time.monotonic()
    assert setting.current() == "blob" and time.monotonic() - started < 0.2
    deadline = time.monotonic() + 3
    while setting.current() != "databricks" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert setting.current() == "databricks"


# --- result tagging / mismatch ------------------------------------------------------

def test_results_status_mismatch_rules():
    setting = {"source": "databricks"}
    blob_snapshot = {"scan_id": "s1", "generated_at": "t", "features_source": "blob",
                     "features_source_detail": {"scanned_at": "2026-10-11T08:00:00+00:00"}}
    status = results_status(setting, blob_snapshot, scan_running=False)
    assert status["mismatch"] and status["rescan_required"]
    assert status["results_source"] == "blob" and status["results_scanned_at"] == "2026-10-11T08:00:00+00:00"
    assert results_status(setting, blob_snapshot, scan_running=True)["rescan_required"] is False
    legacy = results_status(setting, {"scan_id": "old", "generated_at": "t"}, scan_running=False)
    assert legacy["results_source"] == "blob" and legacy["results_source_inferred"] and legacy["mismatch"]
    empty = results_status(setting, {"scan_id": "not-scanned"}, scan_running=False)
    assert empty["results_source"] is None and not empty["mismatch"]
    same = results_status({"source": "blob"}, blob_snapshot, scan_running=False)
    assert not same["mismatch"] and same["notes"] == []


# --- API ---------------------------------------------------------------------------

@pytest.fixture()
def api(monkeypatch, tmp_path):
    settings = Settings(state_path=tmp_path / "state.json")
    repository = ReportRepository(settings)
    setting = FeaturesSourceSetting(JsonControlStore(tmp_path / "control.json"), environ={}, load_in_background=False)
    monkeypatch.setattr(app_module, "repository", repository)
    monkeypatch.setattr(app_module, "features_source_setting", setting)
    return SimpleNamespace(client=Client(app_module.app), repository=repository, setting=setting)


def test_api_get_put_validation_and_persistence(api):
    got = api.client.get("/api/settings/features-source").json()
    assert got["source"] == "blob" and got["options"] == ["blob", "databricks"] and got["mismatch"] is False
    bad = api.client.put("/api/settings/features-source", json={"source": "s3"})
    assert bad.status_code == 422
    put = api.client.put("/api/settings/features-source", json={"source": "databricks"},
                         headers={"X-Forwarded-Email": "nitai@augury.com"})
    assert put.status_code == 200
    body = put.json()
    assert body["source"] == "databricks" and body["updated_by"] == "nitai@augury.com"
    assert body["persistence"]["durable"] is True and body["persistence"]["location"].startswith("local_json:")
    assert SCHEMA_UNAVAILABLE_NOTE in body["notes"]


def test_status_reports_mismatch_until_a_new_scan_completes(api):
    api.repository._latest = {"scan_id": "s1", "generated_at": "2026-10-11T07:00:00+00:00", "machines": [],
                              "features_source": "blob", "features_source_detail": {"scanned_at": "2026-10-11T07:00:00+00:00"}}
    api.client.put("/api/settings/features-source", json={"source": "databricks"})
    block = api.client.get("/api/backfill/status").json()["data_sources"]["features_source"]
    assert block["active_source"] == "databricks" and block["results_source"] == "blob"
    assert block["mismatch"] is True and block["rescan_required"] is True
    # A completed scan from the new source clears it.
    api.repository._latest = {**api.repository._latest, "scan_id": "s2", "features_source": "databricks",
                              "features_source_detail": {"scanned_at": "2026-10-11T08:00:00+00:00"}}
    block = api.client.get("/api/backfill/status").json()["data_sources"]["features_source"]
    assert block["mismatch"] is False and block["results_scanned_at"] == "2026-10-11T08:00:00+00:00"


def test_switch_mid_scan_is_409_and_does_not_change_the_source(api):
    api.repository._scan_state = ScanState(running=True)
    response = api.client.put("/api/settings/features-source", json={"source": "databricks"})
    assert response.status_code == 409 and "scan is running" in response.json()["detail"]
    assert api.setting.current() == "blob"


def test_switch_is_not_gated_as_a_workflow_mutation(api, monkeypatch):
    # Admin gating (e.g. Databricks without the Outerbounds SP) never blocks the source switch.
    monkeypatch.setattr(app_module, "RUNTIME_INFO", SimpleNamespace(workflow_mutations=False, workflow_mutations_status_code=503,
                                                                   workflow_mutations_error="x", workflow_mutations_error_type="y", mode="databricks"))
    assert not app_module._workflow_mutation_route("PUT", "/api/settings/features-source")
    assert api.client.put("/api/settings/features-source", json={"source": "databricks"}).status_code == 200


def test_scan_start_tags_in_progress_snapshot_with_the_active_source(api, monkeypatch):
    api.setting.switch("databricks", actor="t")
    monkeypatch.setattr(api.repository, "run_scan", lambda *_: None)
    assert api.client.post("/api/backfill/scan").status_code == 200
    assert api.repository.latest()["snapshot"]["features_source"] == "databricks"
    block = api.client.get("/api/settings/features-source").json()
    assert block["scan_running"] is True and block["rescan_required"] is False


# --- scanner: databricks mode -----------------------------------------------------------

def test_databricks_mode_coverage_row_counts_and_needs_backfill(monkeypatch):
    sql = FakeSql()
    snapshot, store = _scan(monkeypatch, "databricks", sql)
    assert store.calls == 0  # blob is never read in databricks mode
    months = _months(snapshot)
    assert months[(2026, 1)].status == "backfilled" and months[(2026, 1)].row_count == 1000
    assert months[(2026, 2)].status == "needs_backfill" and months[(2026, 2)].zero_count_features == [V2]
    assert months[(2026, 3)].status == "needs_backfill" and months[(2026, 3)].partial_features == [V2]
    assert months[(2026, 3)].feature_coverage_gaps[V2]["missing_rows"] == 10
    assert months[(2026, 4)].status == "needs_backfill" and months[(2026, 4)].activity_status == "offline"
    assert months[(2026, 5)].status == "backfilled"
    machine = snapshot.machines[0]
    assert machine.production_rows == 2400 and machine.coverage_start_month == "2026-01"
    assert snapshot.features_source == "databricks"
    assert snapshot.features_source_detail["schema_check"] == "not_available"
    assert snapshot.features_source_detail["table"] == TABLE
    # Statements: column probe, bronze versions, one aggregate (no blob, no per-month queries).
    assert len(sql.queries) == 3
    assert months[(2026, 1)].last_modified.startswith("2026-01-28") and months[(2026, 1)].source_detail["last_modified_source"] == "databricks_bronze"


def test_databricks_mode_never_reports_missing_schema_columns(monkeypatch):
    blob_snapshot, _ = _scan(monkeypatch, "blob")
    assert _months(blob_snapshot)[(2026, 1)].missing_schema_columns == ["absent_in_dbx"]
    assert _months(blob_snapshot)[(2026, 1)].status == "needs_backfill"
    snapshot, _ = _scan(monkeypatch, "databricks", FakeSql())
    month = _months(snapshot)[(2026, 1)]
    assert month.missing_schema_columns == [] and month.status == "backfilled"
    assert month.source_detail["schema_check"] == "not_available"
    assert SCHEMA_UNAVAILABLE_NOTE in snapshot.warnings
    assert not any("Full-schema completeness is checked" in warning for warning in snapshot.warnings)


def test_databricks_mode_flags_stale_risk_for_older_months(monkeypatch):
    snapshot, _ = _scan(monkeypatch, "databricks", FakeSql())
    assert _months(snapshot)[(2026, 1)].source_detail["stale_risk"] is True
    assert "verify on Blob before backfilling" in _months(snapshot)[(2026, 2)].reason
    assert "verify on Blob" not in _months(snapshot)[(2026, 1)].reason  # backfilled months are not annotated
    assert any("may be stale for older backfilled months" in warning for warning in snapshot.warnings)


def test_databricks_timeout_fails_open_without_inventing_gaps(monkeypatch):
    snapshot, _ = _scan(monkeypatch, "databricks", FakeSql(delay=1.0), timeout=0.2)
    months = _months(snapshot)
    assert {month.status for month in months.values()} == {"scan_error"}
    assert all("Databricks feature_store query failed" in month.reason for month in months.values())
    assert snapshot.features_source_detail["error"].startswith("TimeoutError")
    assert snapshot.summary.needs_backfill == 0


def test_databricks_sql_error_fails_open(monkeypatch):
    snapshot, _ = _scan(monkeypatch, "databricks", FakeSql(error=RuntimeError("warehouse stopped token=abc")))
    assert {month.status for month in _months(snapshot).values()} == {"scan_error"}
    assert "token=<redacted>" in snapshot.features_source_detail["error"]


def test_target_feature_absent_from_table_is_not_judged(monkeypatch):
    snapshot, _ = _scan(monkeypatch, "databricks", FakeSql(table_columns=("machine_id", "recorded_at", V1)))
    months = _months(snapshot)
    assert months[(2026, 1)].status == "scan_error" and "not in Databricks feature_store" in months[(2026, 1)].reason
    assert months[(2026, 4)].status == "needs_backfill"  # no rows at all is still a missing month


def test_crosscheck_is_disabled_in_databricks_mode(monkeypatch):
    monkeypatch.setenv("FEATURES_CROSSCHECK", "feature_store")

    def boom(*_args, **_kwargs):
        raise AssertionError("cross-check must not run in databricks mode")

    monkeypatch.setattr(scanner_module, "FeatureStoreCrosscheck", boom)
    snapshot, _ = _scan(monkeypatch, "databricks", FakeSql())
    assert snapshot.crosscheck["status"] == "disabled" and snapshot.crosscheck["enabled"] is False
    assert all(month.crosscheck is None for month in _months(snapshot).values())


def test_coverage_query_pushes_filters_and_counts_gaps():
    query = build_coverage_query(TABLE, [MID], (2024, 8), (2026, 10), [V2], {V2: V1})
    assert f"machine_id IN ('{MID}')" in query
    assert "recorded_at >= TIMESTAMP '2024-08-01 00:00:00'" in query and "recorded_at < TIMESTAMP '2026-11-01 00:00:00'" in query
    assert f"COUNT(`{V2}`) AS `nn__{V2}`" in query
    assert f"SUM(CASE WHEN `{V1}` IS NOT NULL AND `{V2}` IS NULL THEN 1 ELSE 0 END) AS `gap__{V2}`" in query


# --- blob regression ------------------------------------------------------------------------

def test_blob_mode_is_unchanged_and_tagged(monkeypatch):
    sql = FakeSql()
    snapshot, store = _scan(monkeypatch, "blob", sql)
    assert sql.queries == [] and store.calls > 0
    months = _months(snapshot)
    assert months[(2026, 1)].status == "needs_backfill"  # canonical schema check still applies on blob
    assert months[(2026, 2)].zero_count_features == [V2]
    assert months[(2026, 4)].activity_status == "offline"
    assert months[(2026, 1)].source_detail is None and months[(2026, 1)].last_modified == "2026-09-28T12:13:08+00:00"
    assert snapshot.features_source == "blob" and snapshot.features_source_detail["schema_check"] == "available"
    assert any("Full-schema completeness is checked" in warning for warning in snapshot.warnings)


def test_scan_logs_name_the_source(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error.data_source"):
        _scan(monkeypatch, "databricks", FakeSql())
    assert f"features=databricks {TABLE}" in caplog.text
    assert "features_source databricks: table=" in caplog.text


def test_databricks_and_blob_control_stores_use_reserved_setting_keys(monkeypatch):
    from backfill_dashboard.control_store import AzureBlobControlStore, DatabricksControlStore

    statements = []
    store = DatabricksControlStore("dih_prod.mlops.backfill_dashboard_control")
    monkeypatch.setattr(store, "_rows", lambda statement: statements.append(statement) or [])
    store.put_setting("features_source", {"value": "databricks"})
    assert store.get_setting("features_source") is None
    assert "'__setting__features_source' machine_id, -1 month_index" in statements[0]
    assert "machine_id = '__setting__features_source' AND month_index = -1" in statements[1]

    uploads = {}
    blob = SimpleNamespace(upload_blob=lambda data, overwrite: uploads.update(data=data, overwrite=overwrite))
    azure = AzureBlobControlStore("acct", "cont")
    monkeypatch.setattr(azure, "_setting_blob", lambda name: blob)
    azure.put_setting("features_source", {"value": "blob"})
    assert json.loads(uploads["data"]) == {"value": "blob"} and uploads["overwrite"] is True
    assert azure.describe_settings_location("features_source").endswith("dashboard-control/_settings/features_source.json")
    with pytest.raises(ValueError):
        azure.describe_settings_location("../x")
