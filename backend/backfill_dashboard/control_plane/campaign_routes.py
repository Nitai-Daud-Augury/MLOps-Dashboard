from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .limiter import limiter
from .machine_control import apply_machine_action
from .schemas import CampaignActionRequest, CampaignSubmitRequest, EstimateRequest, MachineActionRequest, WorkItemActionRequest
from .security import mutation_actor


def campaign_router(control_plane) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.post("/backfill-estimates")
    def create_estimate(request: EstimateRequest):
        if not limiter.allow("estimate", 0.25):
            raise HTTPException(429, "estimate rate limit exceeded; retry shortly")
        try:
            return control_plane.estimates.create(request.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/backfill-campaigns", status_code=201)
    def submit_campaign(request: CampaignSubmitRequest, http_request: Request):
        if not limiter.allow("submit", 1.0):
            raise HTTPException(429, "submission rate limit exceeded; retry shortly")
        try:
            actor = mutation_actor(http_request, production=request.production)
            return control_plane.campaign_service.submit(
                request.estimate_id, request.estimate_signature, name=request.name, created_by=actor,
                production=request.production, confirmation_text=request.confirmation_text,
                ulrpm_confirmation_text=request.ulrpm_confirmation_text)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/backfill-campaigns")
    def list_campaigns(limit: int = Query(50, ge=1, le=200)):
        return {"campaigns": control_plane.campaigns.list(limit)}

    @router.get("/backfill-campaigns/{campaign_id}")
    def get_campaign(campaign_id: str):
        value = control_plane.campaigns.get(campaign_id)
        if not value:
            raise HTTPException(404, "campaign not found")
        return value

    @router.get("/backfill-campaigns/{campaign_id}/items")
    def get_items(campaign_id: str, cursor: int = 0, limit: int = Query(100, ge=1, le=200)):
        if not control_plane.campaigns.get(campaign_id):
            raise HTTPException(404, "campaign not found")
        return control_plane.campaigns.items(campaign_id, cursor, limit)

    @router.post("/backfill-campaigns/{campaign_id}/actions")
    def campaign_action(campaign_id: str, request: CampaignActionRequest, http_request: Request):
        try:
            actor = mutation_actor(http_request, production=bool((control_plane.campaigns.get(campaign_id) or {}).get("production")))
            return control_plane.campaigns.set_state(campaign_id, request.action, actor, request.reason)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/backfill-campaigns/{campaign_id}/events")
    async def campaign_events(campaign_id: str, after: int = 0):
        async def stream():
            cursor = after
            while True:
                events = control_plane.campaigns.events(campaign_id, cursor)
                for event in events:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {json.dumps(event)}\n\n"
                yield ": keepalive\n\n"
                await asyncio.sleep(5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @router.post("/backfill-campaigns/{campaign_id}/items/{work_item_id}/actions")
    def work_item_action(campaign_id: str, work_item_id: str, request: WorkItemActionRequest, http_request: Request):
        campaign = control_plane.campaigns.get(campaign_id) or {}
        actor = mutation_actor(http_request, production=bool(campaign.get("production")))
        try:
            return control_plane.campaigns.resolve_item(campaign_id, work_item_id, request.action, actor, request.reason)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/backfill-campaigns/{campaign_id}/machines/{machine_id}/actions")
    def machine_action(campaign_id: str, machine_id: str, request: MachineActionRequest, http_request: Request):
        campaign = control_plane.campaigns.get(campaign_id) or {}
        actor = mutation_actor(http_request, production=bool(campaign.get("production")))
        try:
            return apply_machine_action(control_plane.campaigns, campaign_id, machine_id,
                                        request.action, actor, request.reason)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
