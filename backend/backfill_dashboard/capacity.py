from __future__ import annotations

from dataclasses import dataclass, field
from math import floor


@dataclass(frozen=True)
class NodeCapacity:
    name: str
    available_memory_mib: int
    available_cpu_millicores: int
    available_pod_slots: int


@dataclass(frozen=True)
class CapacityRecommendation:
    raw_slots: int
    recommended_concurrency: int
    limiting_factors: dict[str, int]
    safety_factor: float
    confidence: str


def recommend_concurrency(nodes: list[NodeCapacity], *, memory_mib: int, cpu_millicores: int, argo_slots: int, configured_ceiling: int, fst_ceiling: int, dispatcher_ceiling: int, safety_factor: float = 0.60, benchmark_ceiling: int | None = None) -> CapacityRecommendation:
    if min(memory_mib, cpu_millicores, argo_slots, configured_ceiling, fst_ceiling, dispatcher_ceiling) < 0:
        raise ValueError("capacity inputs cannot be negative")
    pool = sum(min(n.available_memory_mib // memory_mib if memory_mib else 0, n.available_cpu_millicores // cpu_millicores if cpu_millicores else 0, n.available_pod_slots) for n in nodes)
    factors = {"pool_slots": pool, "argo_slots": argo_slots, "configured_pool_ceiling": configured_ceiling, "fst_storage_ceiling": fst_ceiling, "dispatcher_ceiling": dispatcher_ceiling}
    raw = min(factors.values())
    if benchmark_ceiling is not None:
        factors["benchmark_ceiling"] = max(0, benchmark_ceiling)
        raw = min(raw, factors["benchmark_ceiling"])
    recommendation = floor(raw * safety_factor)
    return CapacityRecommendation(raw, recommendation, factors, safety_factor, "verified" if nodes else "unverified")
