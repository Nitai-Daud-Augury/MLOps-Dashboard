from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .database import Database

PRIORS = {"standard": (0.35, 0.75, 2.0), "ulrpm": (1.5, 4.0, 10.0)}


class BenchmarkStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def distribution(self, cohort: str, feature_version: str, window_days: int, endpoint_count: int = 1) -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        bucket = _endpoint_bucket(endpoint_count)
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT duration_hours FROM benchmarks WHERE cohort=? AND feature_version=? AND window_days=? AND endpoint_bucket=? AND recorded_at>=? ORDER BY duration_hours",
                (cohort, feature_version, window_days, bucket, cutoff),
            ).fetchall()
        values = [float(row[0]) for row in rows]
        p50, p80, p95 = (_percentile(values, q) for q in (0.5, 0.8, 0.95)) if len(values) >= 5 else PRIORS[cohort]
        return {"p50_hours": p50, "p80_hours": p80, "p95_hours": p95, "sample_count": len(values),
                "confidence": "measured" if len(values) >= 5 else "low", "endpoint_bucket": bucket}

    def record(self, **values) -> None:
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO benchmarks(cohort,resource_profile_version,feature_version,window_days,endpoint_bucket,duration_hours,retry_count,peak_memory_mib,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (values["cohort"], values["resource_profile_version"], values["feature_version"], values["window_days"],
                 _endpoint_bucket(values.get("endpoint_count", 1)), values["duration_hours"], values.get("retry_count", 0),
                 values.get("peak_memory_mib"), datetime.now(timezone.utc).isoformat()),
            )


def _endpoint_bucket(count: int) -> str:
    return "1" if count <= 1 else "2-4" if count <= 4 else "5+"


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = (len(values) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)
