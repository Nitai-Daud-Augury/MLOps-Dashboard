"""Optional FastAPI hosting for the built Vite single-page application."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.routing import Match
from starlette.staticfiles import StaticFiles


LOCAL_VITE_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")
HTTP_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def local_vite_development_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Enable cross-origin dev requests only outside deployed app runtimes."""
    values = os.environ if environ is None else environ
    runtime = values.get("MLOPS_DASHBOARD_RUNTIME")
    return (
        "DATABRICKS_APP_PORT" not in values
        and (runtime is None or runtime.strip().lower() == "local")
    )


def _default_static_root() -> Path:
    # static_host.py -> backfill_dashboard -> backend -> dashboard root
    return Path(__file__).resolve().parents[2] / "dist"


def _configured_static_root(static_dir: Path | str | None) -> Path:
    configured = static_dir if static_dir is not None else os.getenv("MLOPS_DASHBOARD_STATIC_DIR")
    return Path(configured).expanduser().resolve() if configured else _default_static_root().resolve()


def _safe_file(root: Path, request_path: str) -> tuple[Path | None, bool]:
    """Return a contained regular file, plus whether the path was safe."""
    decoded = unquote(request_path)
    if "\x00" in decoded or "\\" in decoded or decoded.startswith("/"):
        return None, False
    parts = Path(decoded).parts
    if any(part in {".", ".."} or part.startswith(".") for part in parts):
        return None, False
    try:
        candidate = (root / Path(*parts)).resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, False
    return (candidate if candidate.is_file() else None), True


def register_static_spa_host(
    app: FastAPI,
    static_dir: Path | str | None = None,
) -> bool:
    """Register asset and SPA routes when a built Vite tree is available.

    Call after all API and WebSocket routes have been registered. Returns False
    without adding routes when the build output is absent or incomplete, which
    keeps backend-only deployments usable.
    """
    root = _configured_static_root(static_dir)
    index_file = (root / "index.html").resolve()
    assets_dir = (root / "assets").resolve()
    try:
        index_file.relative_to(root)
        assets_dir.relative_to(root)
    except ValueError:
        return False
    if not index_file.is_file() or not assets_dir.is_dir():
        return False

    # This route follows every registered API route, so an API path typo gets a
    # JSON 404 while existing API routes and WebSockets retain precedence.
    registered_routes = tuple(app.router.routes)

    async def unknown_api(request: Request) -> Response:
        allowed: set[str] = set()
        for route in registered_routes:
            try:
                match, _ = route.matches(request.scope)
            except (AttributeError, KeyError, TypeError):
                continue
            if match == Match.PARTIAL:
                allowed.update(getattr(route, "methods", set()) or set())
        if allowed:
            return JSONResponse(
                {"detail": "Method Not Allowed"},
                status_code=405,
                headers={"Allow": ", ".join(sorted(allowed))},
            )
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    app.add_api_route("/api", unknown_api, methods=HTTP_METHODS, include_in_schema=False)
    app.add_api_route(
        "/api/{path:path}", unknown_api,
        methods=HTTP_METHODS,
        include_in_schema=False,
    )
    app.mount(
        "/assets",
        StaticFiles(directory=str(assets_dir), check_dir=True),
        name="dashboard-assets",
    )

    async def serve_root() -> FileResponse:
        return FileResponse(index_file)

    async def serve_spa_path(request: Request, path: str) -> Response:
        request_path = request.url.path
        if request_path == "/api" or request_path.startswith("/api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate, safe = _safe_file(root, path)
        if not safe:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if candidate is not None:
            return FileResponse(candidate)
        return FileResponse(index_file)

    app.add_api_route("/", serve_root, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route(
        "/{path:path}", serve_spa_path,
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    return True
