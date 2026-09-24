from __future__ import annotations

import hashlib
import json
import os

from ..capacity import NodeCapacity, recommend_concurrency

PROFILES = {
    "standard": {"memory_mib": 8192, "cpu_millicores": 1000, "pool": "default", "purchase": "spot", "version": "standard-8g-v1"},
    "ulrpm": {"memory_mib": 64000, "cpu_millicores": 4000, "pool": "obp-main-big3", "purchase": "ondemand", "version": "ulrpm-64g-v1"},
}


class CapacityService:
    def snapshot(self) -> dict:
        cohorts = {name: self._cohort(name, profile) for name, profile in PROFILES.items()}
        payload = {"cohorts": cohorts, "production_ready": all(item["confidence"] == "verified" for item in cohorts.values())}
        payload["digest"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return payload

    def _cohort(self, cohort: str, profile: dict) -> dict:
        profile = {**profile, "region": os.getenv("BACKFILL_AZURE_REGION", ""),
                   "sku": os.getenv(f"BACKFILL_{cohort.upper()}_VM_SKU", "Standard_E64ds_v5" if cohort == "ulrpm" else ""),
                   "node_memory_mib": int(os.getenv(f"BACKFILL_{cohort.upper()}_NODE_MEMORY_MIB", "524288" if cohort == "ulrpm" else "0")),
                   "node_cpu_millicores": int(os.getenv(f"BACKFILL_{cohort.upper()}_NODE_CPU_MILLICORES", "64000" if cohort == "ulrpm" else "0"))}
        raw_nodes = os.getenv(f"BACKFILL_{cohort.upper()}_NODES_JSON", "")
        verified = os.getenv(f"BACKFILL_{cohort.upper()}_CAPACITY_VERIFIED", "0") == "1"
        nodes = [NodeCapacity(**item) for item in json.loads(raw_nodes)] if raw_nodes else _illustrative_nodes(cohort)
        ceiling = int(os.getenv(f"BACKFILL_{cohort.upper()}_CEILING", "40" if cohort == "standard" else "5"))
        benchmark = int(os.getenv(f"BACKFILL_{cohort.upper()}_BENCHMARK_CEILING", "9" if cohort == "standard" else "3"))
        result = recommend_concurrency(
            nodes, memory_mib=profile["memory_mib"], cpu_millicores=profile["cpu_millicores"],
            argo_slots=int(os.getenv("BACKFILL_ARGO_SLOTS", "40")), configured_ceiling=ceiling,
            fst_ceiling=int(os.getenv("BACKFILL_FST_CEILING", "20")), dispatcher_ceiling=int(os.getenv("BACKFILL_DISPATCHER_CEILING", "20")),
            safety_factor=float(os.getenv("BACKFILL_CAPACITY_SAFETY_FACTOR", "0.60")), benchmark_ceiling=benchmark,
        )
        return {**profile, "raw_slots": result.raw_slots, "recommended_concurrency": result.recommended_concurrency,
                "limiting_factors": result.limiting_factors, "safety_factor": result.safety_factor,
                "confidence": "verified" if verified and raw_nodes else "unverified"}


def _illustrative_nodes(cohort: str) -> list[NodeCapacity]:
    if cohort == "standard":
        return [NodeCapacity(f"illustrative-{index}", 12288, 2000, 4) for index in range(10)]
    return [NodeCapacity("illustrative-ulrpm", 128000, 8000, 4)]
