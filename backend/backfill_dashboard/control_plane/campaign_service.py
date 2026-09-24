from __future__ import annotations

from datetime import date, datetime, timezone
from datetime import timedelta
from uuid import uuid4

from ..campaigns import plan_machine_windows
from .campaign_repository import CampaignRepository
from .capacity_service import PROFILES, CapacityService
from .estimate_service import config_digest
from .selection import iter_selection
from .signatures import EstimateSigner


class CampaignService:
    def __init__(self, inventory, repository: CampaignRepository, capacity: CapacityService,
                 signer: EstimateSigner, readiness=lambda: {"production_ready": True, "blockers": []}) -> None:
        self.inventory, self.repository, self.capacity, self.signer = inventory, repository, capacity, signer
        self.readiness = readiness

    def submit(self, estimate_id: str, signature: str, *, name: str, created_by: str,
               production: bool, confirmation_text: str, ulrpm_confirmation_text: str) -> dict:
        estimate = self.repository.estimate(estimate_id)
        if not estimate or signature != estimate.get("estimate_signature"):
            raise ValueError("estimate signature does not match")
        signed = {key: estimate[key] for key in ("estimate_id", "expires_at", "inventory_version", "capacity_digest", "request")}
        if not self.signer.valid(signed, signature):
            raise ValueError("estimate signature is invalid")
        if datetime.fromisoformat(estimate["expires_at"]) <= datetime.now(timezone.utc):
            raise ValueError("estimate expired; request a new estimate")
        if estimate["inventory_version"] != self.inventory.get_version():
            raise ValueError("inventory changed; request a new estimate")
        if estimate["capacity_digest"] != self.capacity.snapshot()["digest"]:
            raise ValueError("capacity changed; request a new estimate")
        readiness = self.readiness() if production else {"production_ready": True, "blockers": []}
        if production and not readiness["production_ready"]:
            raise ValueError("production readiness failed: " + "; ".join(readiness["blockers"]))
        if production and (not estimate["production_ready"] or confirmation_text != "RUN_PROD_BACKFILL"):
            raise ValueError("production submission is not verified or confirmation text is incorrect")
        if production and estimate["lanes"]["ulrpm"]["machine_count"] and ulrpm_confirmation_text != "APPROVE_ULRPM_ON_DEMAND":
            raise ValueError("ULRPM on-demand submission requires separate confirmation")
        if production:
            self._validate_quotes(estimate)
        request, campaign_id = estimate["request"], uuid4().hex
        campaign = {"id": campaign_id, "name": name.strip() or f"backfill-{campaign_id[:8]}",
                    "created_by": created_by.strip() or "unknown", "created_at": datetime.now(timezone.utc).isoformat(),
                    "inventory_version": estimate["inventory_version"], "selection_snapshot": request["selection"],
                    "cohort_counts": {key: value["machine_count"] for key, value in estimate["lanes"].items()},
                    "date_start": request["start_at"], "date_end": request["end_at"],
                    "feature_version": request["feature_set_version"], "config_digest": config_digest(request),
                    "estimate_id": estimate_id, "estimate_signature": signature, "production": production}
        self.repository.create(campaign)
        try:
            count = self._plan(campaign, request)
            self.repository.finish_planning(campaign_id, count)
        except Exception as exc:
            self.repository.planning_failed(campaign_id, exc)
            raise
        return self.repository.get(campaign_id) or campaign

    def _plan(self, campaign: dict, request: dict) -> int:
        batch, count = [], 0
        profiles = {name: profile["version"] for name, profile in PROFILES.items()}
        for machine in iter_selection(self.inventory, campaign["selection_snapshot"]):
            if not machine.backfill_eligible or machine.resource_cohort == "unknown":
                continue
            batch.extend(plan_machine_windows(
                machine, campaign_id=campaign["id"], start_at=date.fromisoformat(campaign["date_start"]),
                end_at=date.fromisoformat(campaign["date_end"]), window_days=int(request[f"{machine.resource_cohort}_window_days"]),
                feature_set_version=campaign["feature_version"], config_digest=campaign["config_digest"]))
            if len(batch) >= 1000:
                count += self.repository.add_work(batch, profiles)
                batch.clear()
        return count + self.repository.add_work(batch, profiles)

    @staticmethod
    def _validate_quotes(estimate: dict) -> None:
        for lane in estimate["lanes"].values():
            if not lane["machine_count"]:
                continue
            timestamp = lane["price_quote"].get("retrieved_at")
            if not timestamp or datetime.fromisoformat(timestamp) + timedelta(hours=24) <= datetime.now(timezone.utc):
                raise ValueError("price quote expired; request a new estimate")
