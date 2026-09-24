from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class EstimateRange:
    low: float
    likely: float
    high: float


def makespan_estimate(durations_by_machine: dict[str, list[float]], concurrency: int, retry_factor: float = 1.0) -> EstimateRange:
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    totals = [sum(values) * retry_factor for values in durations_by_machine.values()]
    total = sum(totals)
    critical = max(totals, default=0.0)
    base = max(total / concurrency, critical)
    return EstimateRange(base * 0.75, base, base * 1.5)


def allocated_cost(duration_hours: float, requested_cpu: float, requested_memory: float, node_cpu: float, node_memory: float, hourly_rate: float) -> float:
    if min(duration_hours, requested_cpu, requested_memory, node_cpu, node_memory, hourly_rate) < 0 or not node_cpu or not node_memory:
        raise ValueError("cost inputs must be positive")
    return duration_hours * max(requested_cpu / node_cpu, requested_memory / node_memory) * hourly_rate
