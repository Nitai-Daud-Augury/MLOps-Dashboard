"""Campaign dispatch must take an allowlisted FST namespace and never assume prod."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

from fastapi import FastAPI

from backfill_dashboard.classification import ClassificationResult
from backfill_dashboard.config import (
    DEFAULT_DEV_NAMESPACE,
    PROD_CONFIRMATION,
    PROD_NAMESPACE,
    get_settings,
    require_campaign_namespace,
)
from backfill_dashboard.control_plane.campaign_routes import campaign_router
from backfill_dashboard.control_plane.database import Database
from backfill_dashboard.control_plane.dispatcher import Dispatcher
from backfill_dashboard.control_plane.limiter import limiter
from backfill_dashboard.control_plane.workflow_adapter import ArgoWorkflowAdapter
from test_control_plane import build_plane


def _request(app, method: str, path: str, payload: dict | None = None, headers: dict[str, str] | None = None):
    async def run():
        sent = []
        body = json.dumps(payload or {}).encode()
        received = False
        raw_headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
        raw_headers.extend((key.lower().encode(), value.encode()) for key, value in (headers or {}).items())

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        await app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": method, "scheme": "http",
            "path": unquote(path), "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": raw_headers,
            "client": ("testclient", 50000), "server": ("testserver", 80),
        }, receive, send)
        start = next(item for item in sent if item["type"] == "http.response.start")
        content = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        return SimpleNamespace(status_code=start["status"], body=json.loads(content), text=content.decode())

    return asyncio.run(run())


def _plane(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "backfill_dashboard.inventory_provider.classify_machine",
        lambda _document: ClassificationResult("standard", ("test-standard",), "classifier-v1"),
    )
    plane = build_plane(tmp_path, monkeypatch)
    assert plane.synchronizer.sync()["machine_count"] == 3
    estimate = plane.estimates.create({
        "selection": {"explicit_machine_ids": ["machine-standard"]},
        "start_at": "2026-01-01", "end_at": "2026-01-02", "feature_set_version": "v1",
        "standard_window_days": 1, "ulrpm_window_days": 1,
    })
    return plane, estimate


def _post(plane, estimate, payload: dict, headers: dict[str, str] | None = None):
    limiter._last.clear()
    app = FastAPI()
    app.include_router(campaign_router(plane))
    return _request(app, "POST", "/api/v1/backfill-campaigns", payload, headers)


def _body(estimate, **overrides) -> dict:
    payload = {
        "estimate_id": estimate["estimate_id"],
        "estimate_signature": estimate["estimate_signature"],
        "name": "namespace canary",
        "production": False,
        "confirmation_text": "",
        "ulrpm_confirmation_text": "",
        "namespace": DEFAULT_DEV_NAMESPACE,
    }
    payload.update(overrides)
    if payload.get("namespace") is None:
        payload.pop("namespace")
    return payload


def _allow_production_gates(plane, estimate) -> None:
    plane.campaign_service.readiness = lambda: {"production_ready": True, "blockers": []}
    stored = plane.campaigns.estimate(estimate["estimate_id"])
    stored["production_ready"] = True
    with plane.database.connect() as db:
        db.execute(
            "UPDATE estimates SET result_json=?, production_ready=1 WHERE id=?",
            (json.dumps(stored), estimate["estimate_id"]),
        )


def _dispatch(plane, monkeypatch) -> list[str]:
    monkeypatch.setenv("BACKFILL_STANDARD_WORKFLOW_TEMPLATE", "standard-template-v1")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(stdout=json.dumps({"metadata": {"name": "wf-1", "uid": "uid-1"}}))

    monkeypatch.setattr("backfill_dashboard.control_plane.workflow_adapter.subprocess.run", fake_run)
    writer = SimpleNamespace(write_machine_manifest=lambda _request: SimpleNamespace(manifest_path="manifests/one.json"))
    dispatcher = Dispatcher(
        plane.campaigns, plane.inventory, plane.capacity, ArgoWorkflowAdapter(writer), plane.pricing,
    )
    assert dispatcher.tick() == 1
    assert calls
    return calls[0]


def _namespace_arg(command: list[str]) -> str:
    return next(part for part in command if part.startswith("name-space="))


def test_allowlist_defaults_to_the_known_dev_and_prod_namespaces(monkeypatch):
    monkeypatch.delenv("BACKFILL_CAMPAIGN_NAMESPACE_ALLOWLIST", raising=False)
    assert get_settings().campaign_namespace_allowlist == [DEFAULT_DEV_NAMESPACE, PROD_NAMESPACE]


def test_dev_namespace_reaches_submitted_workflow_params(tmp_path, monkeypatch):
    plane, estimate = _plane(tmp_path, monkeypatch)
    response = _post(plane, estimate, _body(estimate))
    assert response.status_code == 201, response.text
    assert response.body["namespace"] == DEFAULT_DEV_NAMESPACE
    assert response.body["production"] is False
    command = _dispatch(plane, monkeypatch)
    assert _namespace_arg(command) == f'name-space="{DEFAULT_DEV_NAMESPACE}"'
    assert PROD_NAMESPACE not in command


def test_prod_namespace_is_blocked_without_typed_confirmation(tmp_path, monkeypatch):
    plane, estimate = _plane(tmp_path, monkeypatch)
    monkeypatch.setenv("BACKFILL_OPERATOR_TOKEN", "operator-token")
    headers = {"authorization": "Bearer operator-token", "x-authenticated-user": "tester"}
    blocked = _post(plane, estimate, _body(
        estimate, namespace=PROD_NAMESPACE, production=True, confirmation_text="",
    ), headers)
    assert blocked.status_code == 403, blocked.text
    assert PROD_CONFIRMATION in blocked.text
    assert plane.campaigns.list() == []

    wrong = _post(plane, estimate, _body(
        estimate, namespace=PROD_NAMESPACE, production=False, confirmation_text=PROD_CONFIRMATION,
    ), headers)
    assert wrong.status_code == 403, wrong.text
    assert plane.campaigns.list() == []


def test_prod_namespace_with_confirmation_is_submitted_mocked(tmp_path, monkeypatch):
    plane, estimate = _plane(tmp_path, monkeypatch)
    _allow_production_gates(plane, estimate)
    monkeypatch.setenv("BACKFILL_OPERATOR_TOKEN", "operator-token")
    response = _post(plane, estimate, _body(
        estimate, namespace=PROD_NAMESPACE, production=True, confirmation_text=PROD_CONFIRMATION,
    ), {"authorization": "Bearer operator-token", "x-authenticated-user": "tester"})
    assert response.status_code == 201, response.text
    assert response.body["namespace"] == PROD_NAMESPACE
    assert response.body["production"] is True
    command = _dispatch(plane, monkeypatch)
    assert _namespace_arg(command) == f'name-space="{PROD_NAMESPACE}"'


def test_unknown_namespace_is_rejected_by_api_and_adapter(tmp_path, monkeypatch):
    plane, estimate = _plane(tmp_path, monkeypatch)
    response = _post(plane, estimate, _body(estimate, namespace="not-a-real-namespace"))
    assert response.status_code == 422, response.text
    assert "BACKFILL_CAMPAIGN_NAMESPACE_ALLOWLIST" in response.text
    assert plane.campaigns.list() == []

    calls = []
    monkeypatch.setattr(
        "backfill_dashboard.control_plane.workflow_adapter.subprocess.run",
        lambda *args, **kwargs: calls.append(args),
    )
    writer_calls = []
    adapter = ArgoWorkflowAdapter(SimpleNamespace(
        write_machine_manifest=lambda _request: writer_calls.append(_request),
    ))
    try:
        adapter.submit({"namespace": "not-a-real-namespace", "cohort": "standard"})
    except ValueError as exc:
        assert "BACKFILL_CAMPAIGN_NAMESPACE_ALLOWLIST" in str(exc)
    else:
        raise AssertionError("unknown namespace was accepted by the adapter")
    assert calls == [] and writer_calls == []


def test_missing_namespace_does_not_fall_back_to_prod(tmp_path, monkeypatch):
    plane, estimate = _plane(tmp_path, monkeypatch)
    response = _post(plane, estimate, _body(estimate, namespace=None))
    assert response.status_code == 422, response.text
    assert "namespace" in response.text
    assert plane.campaigns.list() == []

    try:
        plane.campaign_service.submit(
            estimate["estimate_id"], estimate["estimate_signature"], name="missing", created_by="tester",
            production=False, confirmation_text="", ulrpm_confirmation_text="", namespace="",
        )
    except ValueError as exc:
        assert "does not default to production" in str(exc)
        assert PROD_NAMESPACE not in str(exc)
    else:
        raise AssertionError("blank namespace was accepted")
    assert plane.campaigns.list() == []

    calls = []
    monkeypatch.setattr(
        "backfill_dashboard.control_plane.workflow_adapter.subprocess.run",
        lambda *args, **kwargs: calls.append(args),
    )
    adapter = ArgoWorkflowAdapter(SimpleNamespace(write_machine_manifest=lambda _request: None))
    try:
        adapter.submit({"cohort": "standard", "resource_profile_version": "standard-8g-v1"})
    except ValueError as exc:
        assert "does not default to production" in str(exc)
    else:
        raise AssertionError("adapter defaulted a missing namespace")
    assert calls == []
    assert require_campaign_namespace(DEFAULT_DEV_NAMESPACE) == DEFAULT_DEV_NAMESPACE


def test_legacy_campaign_rows_gain_empty_namespace_instead_of_prod(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE campaigns (id TEXT PRIMARY KEY, production INTEGER NOT NULL DEFAULT 0)")
    connection.execute("INSERT INTO campaigns(id, production) VALUES ('old-campaign', 0)")
    connection.commit()
    connection.close()
    Database(path)
    with sqlite3.connect(path) as migrated:
        row = migrated.execute("SELECT namespace FROM campaigns WHERE id='old-campaign'").fetchone()
    assert row[0] == ""
    assert row[0] != PROD_NAMESPACE


def test_adapter_no_longer_hardcodes_the_production_namespace():
    source = Path(__file__).resolve().parents[1].joinpath(
        "backfill_dashboard/control_plane/workflow_adapter.py"
    ).read_text()
    assert 'name-space="feature-store-container"' not in source
