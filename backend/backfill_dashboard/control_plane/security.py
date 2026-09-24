from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request


def mutation_actor(request: Request, *, production: bool = False) -> str:
    if os.getenv("BACKFILL_AUTH_MODE", "token") == "proxy":
        actor = request.headers.get("x-authenticated-user", "").strip()
        role = request.headers.get("x-backfill-role", "").strip().lower()
        if not actor or role not in {"operator", "admin"}:
            raise HTTPException(403, "trusted proxy operator identity is required")
        return actor
    expected = os.getenv("BACKFILL_OPERATOR_TOKEN", "")
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
    if production and not expected:
        raise HTTPException(503, "production operator authorization is not configured")
    if expected and not hmac.compare_digest(supplied, expected):
        raise HTTPException(403, "operator authorization is required")
    actor = request.headers.get("x-authenticated-user", "").strip()
    if production and not actor:
        raise HTTPException(403, "authenticated user identity is required for production")
    return actor or "local-dashboard-user"
