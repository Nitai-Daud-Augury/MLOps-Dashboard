from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
import math
import statistics
from typing import Any


ULTRASONIC_FEATURES = (
    "ultrasonic_p2p",
    "ultrasonic_rms",
    "ultrasonic_mad",
    "ultrasonic_hf_rms",
    "ultrasonic_rms_30_35",
    "ultrasonic_rms_35_40",
    "ultrasonic_rms_40_45",
    "ultrasonic_impact_energy",
)


def read_feature_series(
    parquet_bytes: bytes,
    *,
    feature: str,
    max_points: int = 2500,
) -> dict[str, Any]:
    """Read one v1/v2 feature pair from a monthly FST partition."""
    if feature not in ULTRASONIC_FEATURES:
        raise ValueError(f"Unsupported ultrasonic feature: {feature}")
    if not 100 <= max_points <= 5000:
        raise ValueError("max_points must be between 100 and 5000")

    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required to read Feature Store chart data") from exc

    parquet = pq.ParquetFile(BytesIO(parquet_bytes))
    available_columns = set(parquet.schema_arrow.names)
    timestamp_column = "recorded_at"
    v2_feature = f"{feature}_v2"
    missing = [name for name in (timestamp_column, feature, v2_feature) if name not in available_columns]
    if missing:
        raise ValueError(f"Partition is missing chart columns: {', '.join(missing)}")

    dimension_columns = [name for name in ("component_id", "bearing", "plane") if name in available_columns]
    table = parquet.read(columns=[timestamp_column, feature, v2_feature, *dimension_columns])
    timestamps = table[timestamp_column].to_pylist()
    v1_values = table[feature].to_pylist()
    v2_values = table[v2_feature].to_pylist()
    dimension_values = {name: table[name].to_pylist() for name in dimension_columns}
    rows = []
    group_counts: dict[str, dict[str, Any]] = {}
    for row_index, (timestamp, raw_v1, raw_v2) in enumerate(zip(timestamps, v1_values, v2_values)):
        v1 = _finite_number(raw_v1)
        v2 = _finite_number(raw_v2)
        if timestamp is None or (v1 is None and v2 is None):
            continue
        component_id = _dimension_value(dimension_values, "component_id", row_index)
        bearing = _dimension_value(dimension_values, "bearing", row_index)
        plane = _dimension_value(dimension_values, "plane", row_index)
        group_key = _group_key(component_id, bearing, plane)
        group = group_counts.setdefault(
            group_key,
            {
                "key": group_key,
                "label": _group_label(component_id, bearing, plane),
                "component_id": component_id,
                "bearing": bearing,
                "plane": plane,
                "row_count": 0,
            },
        )
        group["row_count"] += 1
        rows.append(
            {
                "recorded_at": _timestamp_text(timestamp),
                "group_key": group_key,
                "v1": v1,
                "v2": v2,
                "ratio": v2 / v1 if v1 not in (None, 0) and v2 is not None else None,
            }
        )
    rows.sort(key=lambda item: item["recorded_at"])

    ratios = [item["ratio"] for item in rows if item["ratio"] is not None]
    median_ratio = statistics.median(ratios) if ratios else None
    sampled = _even_sample(rows, max_points)
    return {
        "feature": feature,
        "v2_feature": v2_feature,
        "row_count": len(rows),
        "sampled_count": len(sampled),
        "v1_non_null": sum(item["v1"] is not None for item in rows),
        "v2_non_null": sum(item["v2"] is not None for item in rows),
        "paired_count": len(ratios),
        "median_ratio": median_ratio,
        "p05_ratio": _percentile(ratios, 0.05),
        "p95_ratio": _percentile(ratios, 0.95),
        "range_start": rows[0]["recorded_at"] if rows else None,
        "range_end": rows[-1]["recorded_at"] if rows else None,
        "groups": sorted(
            group_counts.values(),
            key=lambda item: (str(item["component_id"]), str(item["bearing"]), str(item["plane"])),
        ),
        "points": sampled,
    }


def _finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_text(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _dimension_value(values: dict[str, list[Any]], name: str, index: int) -> Any:
    value = values[name][index] if name in values else None
    as_py = getattr(value, "as_py", None)
    return as_py() if callable(as_py) else value


def _group_key(component_id: Any, bearing: Any, plane: Any) -> str:
    return f"component={component_id or 'unknown'}|bearing={bearing if bearing is not None else 'unknown'}|plane={plane if plane is not None else 'unknown'}"


def _group_label(component_id: Any, bearing: Any, plane: Any) -> str:
    component = str(component_id)
    component_label = f"Component …{component[-6:]}" if component_id else "Unknown component"
    bearing_label = f"bearing {bearing}" if bearing is not None else "unknown bearing"
    plane_label = {0: "R1", 1: "R2", 2: "A"}.get(plane, f"plane {plane}" if plane is not None else "all planes")
    return f"{component_label} · {bearing_label} · {plane_label}"


def _even_sample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[0]]
    indexes = {round(index * (len(rows) - 1) / (limit - 1)) for index in range(limit)}
    return [rows[index] for index in sorted(indexes)]


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
