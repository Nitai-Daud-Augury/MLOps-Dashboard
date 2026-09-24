from __future__ import annotations

from io import BytesIO
from datetime import datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from backfill_dashboard.parquet_inspector import inspect_feature_partition
from backfill_dashboard.storage import AzureBlobRangeReader


class _Download:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def readall(self) -> bytes:
        return self.content


class _RangeBlobClient:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.requests: list[tuple[int, int]] = []

    def download_blob(self, *, offset: int, length: int, max_concurrency: int):
        assert max_concurrency == 1
        self.requests.append((offset, length))
        return _Download(self.content[offset:offset + length])


def test_inspector_uses_footer_metadata_for_feature_null_counts():
    output = BytesIO()
    pq.write_table(
        pa.table({"ultrasonic_p2p_v2": [1.0, None, 3.0], "other": [4, 5, 6]}),
        output,
    )

    # A real Azure range read supplies only the end of the file. This is enough
    # for Parquet metadata and avoids fetching/decompressing feature columns.
    summary = inspect_feature_partition(
        output.getvalue()[-(512 * 1024):],
        ["ultrasonic_p2p_v2", "missing_feature"],
    )

    assert summary.row_count == 3
    assert summary.columns == ["ultrasonic_p2p_v2", "other"]
    assert summary.feature_non_null_counts == {
        "ultrasonic_p2p_v2": 2,
        "missing_feature": 0,
    }


def test_inspector_finds_v2_gaps_on_corresponding_v1_source_rows():
    output = BytesIO()
    start = datetime(2026, 4, 1)
    pq.write_table(
        pa.table(
            {
                "recorded_at": [start + timedelta(days=index) for index in range(4)],
                "ultrasonic_p2p": [1.0, 2.0, None, 4.0],
                "ultrasonic_p2p_v2": [None, None, None, 40.0],
            }
        ),
        output,
    )

    summary = inspect_feature_partition(output.getvalue(), ["ultrasonic_p2p_v2"])

    assert summary.feature_coverage_gaps == {
        "ultrasonic_p2p_v2": {
            "missing_rows": 2,
            "first_missing_at": "2026-04-01T00:00:00",
            "last_missing_at": "2026-04-02T00:00:00",
        }
    }


def test_inspector_range_reads_only_selected_parquet_columns():
    output = BytesIO()
    row_count = 100_000
    columns = {
        "recorded_at": list(range(row_count)),
        "ultrasonic_p2p": [1.0] * row_count,
        "ultrasonic_p2p_v2": [2.0] * row_count,
    }
    # Make the unselected payload meaningfully larger than the footer and
    # selected ultrasonic columns, like the ~1,000-column production schema.
    columns.update({
        f"unselected_payload_{column}": [
            f"column-{column}-row-{index:08d}-payload" for index in range(row_count)
        ]
        for column in range(5)
    })
    pq.write_table(
        pa.table(columns),
        output,
        row_group_size=10_000,
    )
    content = output.getvalue()
    client = _RangeBlobClient(content)
    reader = AzureBlobRangeReader(client, len(content))

    summary = inspect_feature_partition(reader, ["ultrasonic_p2p_v2"])

    assert summary.row_count == row_count
    assert summary.feature_coverage_gaps == {}
    requested_bytes = sum(length for _, length in client.requests)
    assert requested_bytes < len(content) / 2
    assert all(length < len(content) for _, length in client.requests)


def test_inspector_uses_footer_only_when_v2_is_non_null_for_every_row():
    output = BytesIO()
    pq.write_table(
        pa.table(
            {
                "recorded_at": list(range(50_000)),
                "ultrasonic_p2p": [1.0] * 50_000,
                "ultrasonic_p2p_v2": [2.0] * 50_000,
                "unselected": [f"payload-{index}" for index in range(50_000)],
            }
        ),
        output,
        row_group_size=5_000,
    )
    content = output.getvalue()
    client = _RangeBlobClient(content)

    summary = inspect_feature_partition(
        AzureBlobRangeReader(client, len(content)),
        ["ultrasonic_p2p_v2"],
    )

    assert summary.feature_coverage_gaps == {}
    # PyArrow's footer probe is 64 KiB; no feature column chunks are needed.
    assert sum(length for _, length in client.requests) <= 64 * 1024
