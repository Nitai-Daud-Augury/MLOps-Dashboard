from __future__ import annotations

import math
import os

from .selection import iter_selection


def aggregate_lanes(inventory, benchmarks, pricing, request: dict, selection: dict, start, end, capacity: dict) -> dict:
    lanes = {cohort: empty_lane(cohort, capacity["cohorts"][cohort]) for cohort in ("standard", "ulrpm")}
    excluded = 0
    feature = request["feature_set_version"]
    distributions: dict[tuple, dict] = {}
    for machine in iter_selection(inventory, selection):
        if not machine.backfill_eligible or machine.resource_cohort == "unknown":
            excluded += 1
            continue
        lane = lanes[machine.resource_cohort]
        days = int(request[f"{machine.resource_cohort}_window_days"])
        windows = math.ceil((end - start).days / days)
        bucket = "1" if machine.endpoint_count <= 1 else "2-4" if machine.endpoint_count <= 4 else "5+"
        key = (machine.resource_cohort, feature, days, bucket)
        distribution = distributions.get(key)
        if distribution is None:
            distribution = benchmarks.distribution(machine.resource_cohort, feature, days, machine.endpoint_count)
            distributions[key] = distribution
        lane["machine_count"] += 1
        lane["work_item_count"] += windows
        keys = ("p50_hours", "p80_hours", "p95_hours")
        lane["critical_hours"] = [max(lane["critical_hours"][i], windows * distribution[key]) for i, key in enumerate(keys)]
        lane["pod_hours"] = [lane["pod_hours"][i] + windows * distribution[key] for i, key in enumerate(keys)]
        lane["benchmark_confidence"] = "low" if distribution["confidence"] == "low" else lane["benchmark_confidence"]
    for lane in lanes.values():
        lane["excluded_count"] = excluded
        finalize_lane(lane, pricing)
    lanes["ulrpm"]["excluded_count"] = 0
    return lanes


def finalize_lane(lane: dict, pricing) -> None:
    profile = lane["resource_profile"]
    concurrency = profile["recommended_concurrency"]
    lane["blocked"] = concurrency == 0
    retry_factors = [float(value) for value in os.getenv("BACKFILL_RETRY_FACTORS", "1.0,1.1,1.3").split(",")]
    if len(retry_factors) != 3 or any(value < 1 for value in retry_factors):
        raise ValueError("BACKFILL_RETRY_FACTORS must contain three values >= 1")
    startup = max(0.0, float(os.getenv("BACKFILL_SCHEDULER_OVERHEAD_HOURS", "0.08")))
    adjusted_pod_hours = [hours * retry_factors[i] for i, hours in enumerate(lane["pod_hours"])]
    lane["completion_hours"] = [max(hours / concurrency, lane["critical_hours"][i]) + startup if concurrency else None for i, hours in enumerate(adjusted_pod_hours)]
    # Estimates must obtain a quote when one is not already cached. Previously
    # this call only read the cache, so every fresh deployment rendered costs
    # as “Unavailable” despite having a valid region and SKU.
    quote = pricing.get_rate(profile["region"], profile["sku"], profile["purchase"], refresh=True) if profile["region"] and profile["sku"] else {"available": False, "hourly_rate": None, "source": "unavailable"}
    lane["price_quote"] = quote
    share = max(profile["memory_mib"] / profile["node_memory_mib"] if profile["node_memory_mib"] else 0,
                profile["cpu_millicores"] / profile["node_cpu_millicores"] if profile["node_cpu_millicores"] else 0)
    lane["allocated_cost"] = [round(hours * share * quote["hourly_rate"], 2) for hours in adjusted_pod_hours] if quote.get("available") and share else [None, None, None]
    nodes = math.ceil(concurrency * share) if concurrency and share else 0
    lane["node_cost"] = [round(nodes * hours * quote["hourly_rate"], 2) if hours is not None else None for hours in lane["completion_hours"]] if quote.get("available") and nodes else [None, None, None]
    lane["storage_cost"] = None
    lane["cost_assumptions"] = {"retry_factors": retry_factors, "scheduler_overhead_hours": startup,
                                "storage_cost": "unknown and excluded"}
    lane["confidence"] = "low" if profile["confidence"] != "verified" or lane["benchmark_confidence"] == "low" or not quote.get("available") else "high"


def empty_lane(cohort: str, profile: dict) -> dict:
    return {"cohort": cohort, "machine_count": 0, "work_item_count": 0, "pod_hours": [0.0, 0.0, 0.0],
            "critical_hours": [0.0, 0.0, 0.0], "resource_profile": profile, "benchmark_confidence": "measured", "excluded_count": 0}
