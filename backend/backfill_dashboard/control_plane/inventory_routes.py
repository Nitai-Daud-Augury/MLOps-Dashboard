from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request, Response

from ..inventory_models import MachineSearchQuery


def inventory_router(control_plane) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/machines/{machine_id}")
    def get_machine(machine_id: str):
        rows = control_plane.inventory.get_many([machine_id])
        if not rows:
            raise HTTPException(404, "machine not found")
        return asdict(rows[0])

    @router.post("/inventory/refresh")
    def refresh_inventory():
        threading.Thread(target=control_plane.synchronizer.sync, name="inventory-refresh", daemon=True).start()
        return {**control_plane.synchronizer.status(), "refreshing": True}

    @router.get("/inventory/readiness")
    def inventory_readiness():
        return {**control_plane.synchronizer.status(), **control_plane.readiness()}

    return router


def etag_response(request: Request, response: Response, payload: dict) -> dict | Response:
    digest = '"' + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest() + '"'
    response.headers["ETag"] = digest
    response.headers["Cache-Control"] = "private, max-age=15, stale-while-revalidate=60"
    if request.headers.get("if-none-match") == digest:
        return Response(status_code=304, headers={"ETag": digest})
    return payload
