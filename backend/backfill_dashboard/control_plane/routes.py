from fastapi import APIRouter

from .campaign_routes import campaign_router
from .inventory_routes import inventory_router
from .status_routes import status_router


def build_router(control_plane) -> APIRouter:
    router = APIRouter()
    # FastAPI 0.129 keeps included routers as lazy placeholders. Flatten this
    # small domain router so including it in the legacy application is stable
    # across the old and new router implementations.
    for child in (inventory_router(control_plane), campaign_router(control_plane), status_router(control_plane)):
        router.routes.extend(child.routes)
    return router
