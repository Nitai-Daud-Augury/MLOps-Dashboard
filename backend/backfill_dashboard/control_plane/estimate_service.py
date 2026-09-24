from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from .benchmark_store import BenchmarkStore
from .campaign_repository import CampaignRepository
from .capacity_service import CapacityService
from .pricing_provider import AzurePricingProvider
from .selection import normalized_selection
from .signatures import EstimateSigner
from .estimate_calculator import aggregate_lanes


class EstimateService:
    def __init__(self, inventory, campaigns: CampaignRepository, capacity: CapacityService, pricing: AzurePricingProvider,
                 benchmarks: BenchmarkStore, signer: EstimateSigner, ttl_seconds: int, inventory_health) -> None:
        self.inventory, self.campaigns, self.capacity = inventory, campaigns, capacity
        self.pricing, self.benchmarks, self.signer = pricing, benchmarks, signer
        self.ttl_seconds, self.inventory_health = ttl_seconds, inventory_health

    def create(self, request: dict) -> dict:
        start, end = date.fromisoformat(request["start_at"]), date.fromisoformat(request["end_at"])
        if end <= start or (end - start).days > 3660:
            raise ValueError("date range must be positive and at most ten years")
        inventory_version = self.inventory.get_version()
        selection = normalized_selection(request["selection"], inventory_version)
        capacity = self.capacity.snapshot()
        lanes = aggregate_lanes(self.inventory, self.benchmarks, self.pricing, request, selection, start, end, capacity)
        if not sum(lane["machine_count"] for lane in lanes.values()):
            raise ValueError("selection contains no eligible machines")
        excluded = sum(lane.pop("excluded_count") for lane in lanes.values())
        machines = sum(lane["machine_count"] for lane in lanes.values())
        work_items = sum(lane["work_item_count"] for lane in lanes.values())
        if machines > int(os.getenv("BACKFILL_MAX_CAMPAIGN_MACHINES", "50000")) or work_items > int(os.getenv("BACKFILL_MAX_WORK_ITEMS", "1000000")):
            raise ValueError("campaign exceeds configured machine or work-item limit")
        created = datetime.now(timezone.utc)
        normalized_request = {**request, "selection": selection}
        result = {"estimate_id": uuid4().hex, "created_at": created.isoformat(),
                  "expires_at": (created + timedelta(seconds=self.ttl_seconds)).isoformat(),
                  "inventory_version": inventory_version, "capacity_digest": capacity["digest"],
                  "capacity": capacity, "lanes": lanes, "excluded_unknown_or_ineligible": excluded,
                  "request": normalized_request, "disclaimer": "Planning estimate, not invoice.",
                  "production_ready": self._production_ready(capacity, lanes)}
        signed = {key: result[key] for key in ("estimate_id", "expires_at", "inventory_version", "capacity_digest", "request")}
        result["estimate_signature"] = self.signer.sign(signed)
        self.campaigns.save_estimate(result)
        return result

    def _production_ready(self, capacity: dict, lanes: dict) -> bool:
        selected = [lane for lane in lanes.values() if lane["machine_count"]]
        health = self.inventory_health()
        policy_verified = os.getenv("BACKFILL_ELIGIBILITY_POLICY_VERIFIED", "0") == "1"
        return bool(selected) and policy_verified and capacity["production_ready"] and self.signer.production_ready and health["status"] == "healthy" and health.get("source") == "local_file" and all(not lane["blocked"] and lane["price_quote"].get("available") and lane["benchmark_confidence"] != "low" for lane in selected)
def config_digest(request: dict) -> str:
    keys = ("feature_set_version", "standard_window_days", "ulrpm_window_days")
    return hashlib.sha256(json.dumps({key: request[key] for key in keys}, sort_keys=True).encode()).hexdigest()
