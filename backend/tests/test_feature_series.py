from __future__ import annotations

from datetime import datetime, timedelta
from io import BytesIO

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backfill_dashboard.feature_series import read_feature_series


def parquet_bytes(row_count: int = 5, *, with_dimensions: bool = False) -> bytes:
    output = BytesIO()
    start = datetime(2026, 2, 1)
    columns = {
        "recorded_at": [start + timedelta(hours=index) for index in range(row_count)],
        "ultrasonic_p2p": [float(index + 1) for index in range(row_count)],
        "ultrasonic_p2p_v2": [float((index + 1) * 10) for index in range(row_count)],
    }
    if with_dimensions:
        columns.update({
            "component_id": ["component-abcdef"] * row_count,
            "bearing": [0] * row_count,
            "plane": [index % 2 for index in range(row_count)],
        })
    pq.write_table(pa.table(columns), output)
    return output.getvalue()


def test_reads_v1_v2_values_and_ratio_statistics():
    result = read_feature_series(parquet_bytes(), feature="ultrasonic_p2p", max_points=100)

    assert result["row_count"] == 5
    assert result["paired_count"] == 5
    assert result["median_ratio"] == 10
    assert result["points"][0]["recorded_at"] == "2026-02-01T00:00:00"
    assert result["points"][-1]["ratio"] == 10


def test_rejects_missing_pair_column():
    with pytest.raises(ValueError, match="ultrasonic_rms"):
        read_feature_series(parquet_bytes(), feature="ultrasonic_rms", max_points=100)


def test_even_sampling_keeps_first_and_last_values():
    result = read_feature_series(parquet_bytes(250), feature="ultrasonic_p2p", max_points=100)

    assert result["sampled_count"] == 100
    assert result["points"][0]["v1"] == 1
    assert result["points"][-1]["v1"] == 250


def test_returns_component_bearing_and_plane_filter_groups():
    result = read_feature_series(parquet_bytes(6, with_dimensions=True), feature="ultrasonic_p2p", max_points=100)

    assert [group["plane"] for group in result["groups"]] == [0, 1]
    assert result["groups"][0]["label"] == "Component …abcdef · bearing 0 · R1"
    assert {point["group_key"] for point in result["points"]} == {group["key"] for group in result["groups"]}
