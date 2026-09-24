from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from .capacity_service import PROFILES
from .metrics import control_plane_metrics
from .inventory_routes import etag_response
from .schemas import BenchmarkRecordRequest
from .security import mutation_actor


def status_router(control_plane) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/capacity")
    def capacity(request: Request, response: Response):
        return etag_response(request, response, control_plane.capacity.snapshot())

    @router.get("/pricing")
    def pricing(request: Request, response: Response, refresh: bool = False):
        snapshot = control_plane.capacity.snapshot()
        payload = {cohort: control_plane.pricing.get_rate(snapshot["cohorts"][cohort]["region"], snapshot["cohorts"][cohort]["sku"], profile["purchase"], refresh)
                   if snapshot["cohorts"][cohort]["region"] and snapshot["cohorts"][cohort]["sku"]
                   else {"available": False, "source": "unavailable"} for cohort, profile in PROFILES.items()}
        return etag_response(request, response, payload)

    @router.get("/runtime-benchmarks")
    def benchmarks(request: Request, response: Response, feature_set_version: str = "default", standard_window_days: int = 30, ulrpm_window_days: int = 5):
        payload = {"standard": control_plane.benchmarks.distribution("standard", feature_set_version, standard_window_days),
                   "ulrpm": control_plane.benchmarks.distribution("ulrpm", feature_set_version, ulrpm_window_days)}
        return etag_response(request, response, payload)

    @router.post("/runtime-benchmarks", status_code=status.HTTP_201_CREATED)
    def record_benchmark(payload: BenchmarkRecordRequest, request: Request):
        actor = mutation_actor(request, production=True)
        values = payload.model_dump()
        values["feature_version"] = values.pop("feature_set_version")
        control_plane.benchmarks.record(**values)
        return {"recorded": True, "actor": actor}

    @router.get("/control-plane/status")
    def control_status():
        return {**control_plane.readiness(), "inventory": control_plane.synchronizer.status()}

    @router.get("/control-plane/metrics")
    def metrics():
        return control_plane_metrics(control_plane.database)

    return router
