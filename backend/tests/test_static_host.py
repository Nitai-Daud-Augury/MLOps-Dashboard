from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import unquote

from fastapi import FastAPI, WebSocket

from backfill_dashboard.static_host import (
    LOCAL_VITE_ORIGINS,
    local_vite_development_enabled,
    register_static_spa_host,
)


def _request(app, path: str, method: str = "GET"):
    async def run():
        sent = []
        received = False
        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        raw_path = path.encode("ascii")
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": method, "scheme": "http",
            "path": unquote(path), "raw_path": raw_path, "query_string": b"",
            "root_path": "", "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000), "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        start = next(message for message in sent if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        headers = {key.decode("latin-1"): value.decode("latin-1") for key, value in start["headers"]}
        return SimpleNamespace(status_code=start["status"], headers=headers, body=body, text=body.decode("utf-8"))

    return asyncio.run(run())


def _websocket_events(app, path: str):
    async def run():
        sent = []
        received = False
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "websocket.connect"}
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            sent.append(message)

        await app({
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "scheme": "ws", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [(b"host", b"testserver")],
            "client": ("testclient", 50000), "server": ("testserver", 80), "subprotocols": [],
        }, receive, send)
        return sent

    return asyncio.run(run())


def _built_frontend(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>MLOps Dashboard</title>", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log('built')", encoding="utf-8")
    return root


def test_static_host_preserves_api_root_assets_spa_and_websocket_routes(tmp_path):
    static_root = _built_frontend(tmp_path)
    app = FastAPI()

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.post("/api/update")
    async def update():
        return {"updated": True}

    @app.websocket("/api/socket")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("connected")

    assert register_static_spa_host(app, static_root)
    assert json.loads(_request(app, "/api/health").body) == {"ok": True}
    method_mismatch = _request(app, "/api/update", method="GET")
    assert method_mismatch.status_code == 405
    assert json.loads(method_mismatch.body) == {"detail": "Method Not Allowed"}
    assert _request(app, "/").text == "<!doctype html><title>MLOps Dashboard</title>"
    assert _request(app, "/favicon.svg").text == "<svg></svg>"
    assert _request(app, "/assets/index-abc123.js").text == "console.log('built')"
    assert _request(app, "/machine/example/backfill").text == "<!doctype html><title>MLOps Dashboard</title>"
    websocket_events = _websocket_events(app, "/api/socket")
    assert {event["type"] for event in websocket_events} >= {"websocket.accept", "websocket.send"}


def test_unknown_api_is_json_404_not_spa_html(tmp_path):
    app = FastAPI()
    assert register_static_spa_host(app, _built_frontend(tmp_path))

    response = _request(app, "/api/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert json.loads(response.body) == {"detail": "Not Found"}

    post_response = _request(app, "/api/also-missing", method="POST")
    assert post_response.status_code == 404
    assert post_response.headers["content-type"].startswith("application/json")
    assert json.loads(post_response.body) == {"detail": "Not Found"}


def test_static_host_rejects_path_traversal(tmp_path):
    static_root = _built_frontend(tmp_path)
    outside_file = tmp_path / "outside-secret.txt"
    outside_file.write_text("do not serve", encoding="utf-8")
    app = FastAPI()
    assert register_static_spa_host(app, static_root)

    response = _request(app, "/%2e%2e/outside-secret.txt")

    assert response.status_code == 404
    assert "do not serve" not in response.text


def test_missing_or_incomplete_dist_leaves_backend_only_app_clean(tmp_path, monkeypatch):
    missing_app = FastAPI()
    assert not register_static_spa_host(missing_app, tmp_path / "missing-dist")
    missing_response = _request(missing_app, "/")
    assert missing_response.status_code == 404
    assert json.loads(missing_response.body) == {"detail": "Not Found"}

    incomplete = tmp_path / "incomplete-dist"
    incomplete.mkdir()
    (incomplete / "index.html").write_text("index", encoding="utf-8")
    assert not register_static_spa_host(FastAPI(), incomplete)

    configured = _built_frontend(tmp_path / "configured")
    monkeypatch.setenv("MLOPS_DASHBOARD_STATIC_DIR", str(configured))
    assert register_static_spa_host(FastAPI())


def test_local_vite_cors_is_disabled_for_deployed_or_nonlocal_runtime():
    assert local_vite_development_enabled({})
    assert local_vite_development_enabled({"MLOPS_DASHBOARD_RUNTIME": "local"})
    assert not local_vite_development_enabled({"DATABRICKS_APP_PORT": "8000"})
    assert not local_vite_development_enabled({"MLOPS_DASHBOARD_RUNTIME": "docker"})
    assert not local_vite_development_enabled({"MLOPS_DASHBOARD_RUNTIME": "databricks"})
    assert LOCAL_VITE_ORIGINS == ("http://localhost:5173", "http://127.0.0.1:5173")
