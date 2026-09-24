from __future__ import annotations

import os

from .benchmark_store import BenchmarkStore
from .campaign_repository import CampaignRepository
from .campaign_service import CampaignService
from .capacity_service import CapacityService
from .database import Database
from .dispatcher import Dispatcher
from .estimate_service import EstimateService
from .inventory_repository import InventoryRepository
from .inventory_sync import InventorySynchronizer
from .pricing_provider import AzurePricingProvider
from .runtime import ControlPlaneRuntime
from .signatures import EstimateSigner
from .workflow_adapter import ArgoWorkflowAdapter


class ControlPlane:
    def __init__(self, settings, source_inventory, source_status: str, manifest_writer,
                 workflow_mutations_enabled: bool = True) -> None:
        self.settings = settings
        self.database = Database(settings.control_plane_db_path)
        self.inventory = InventoryRepository(self.database)
        self.synchronizer = InventorySynchronizer(source_inventory, self.inventory, self.database, source_status)
        self.campaigns = CampaignRepository(self.database)
        self.capacity = CapacityService()
        self.pricing = AzurePricingProvider(self.database)
        self.benchmarks = BenchmarkStore(self.database)
        self.signer = EstimateSigner(settings.control_plane_db_path)
        self.estimates = EstimateService(self.inventory, self.campaigns, self.capacity, self.pricing,
                                         self.benchmarks, self.signer, settings.estimate_ttl_seconds, self.synchronizer.status)
        self.campaign_service = CampaignService(
            self.inventory, self.campaigns, self.capacity, self.signer, self.readiness
        )
        dispatcher = Dispatcher(self.campaigns, self.inventory, self.capacity, ArgoWorkflowAdapter(manifest_writer), self.pricing) if settings.dispatch_enabled and workflow_mutations_enabled else None
        self.runtime = ControlPlaneRuntime(self.synchronizer, dispatcher,
                                           int(os.getenv("BACKFILL_INVENTORY_SYNC_SECONDS", "1800")),
                                           int(os.getenv("BACKFILL_DISPATCH_SECONDS", "3")))

    def readiness(self) -> dict:
        durable = not str(self.settings.control_plane_db_path).startswith("/tmp/")
        blockers = []
        if not durable:
            blockers.append("production control-plane database cannot use /tmp")
        if not self.signer.production_ready:
            blockers.append("BACKFILL_ESTIMATE_SIGNING_KEY is not configured")
        if os.getenv("BACKFILL_AUTH_MODE", "token") != "proxy" and not os.getenv("BACKFILL_OPERATOR_TOKEN"):
            blockers.append("operator authorization is not configured")
        if not self.capacity.snapshot()["production_ready"]:
            blockers.append("live capacity facts are unverified")
        if os.getenv("BACKFILL_ELIGIBILITY_POLICY_VERIFIED", "0") != "1":
            blockers.append("machine eligibility policy is unverified")
        if self.synchronizer.status().get("source") != "local_file" or self.synchronizer.status().get("status") != "healthy":
            blockers.append("the curated ULRPM inventory file is not healthy")
        return {"dispatch_enabled": self.settings.dispatch_enabled, "durable_path": durable,
                "production_ready": not blockers, "blockers": blockers}
