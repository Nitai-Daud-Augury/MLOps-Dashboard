from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import BinaryIO


@dataclass(frozen=True)
class ParquetFeatureSummary:
    row_count: int
    columns: list[str]
    feature_non_null_counts: dict[str, int]
    feature_coverage_gaps: dict[str, dict[str, int | str]] = field(default_factory=dict)


def inspect_feature_partition(content: bytes | BinaryIO, target_features: list[str]) -> ParquetFeatureSummary:
    try:
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("pyarrow is required to inspect Feature Store parquet files.") from exc

    reader = BytesIO(content) if isinstance(content, bytes) else content
    reader.seek(0)
    parquet_file = pq.ParquetFile(reader)
    use_threads = bool(getattr(reader, "_parquet_use_threads", True))
    row_count = parquet_file.metadata.num_rows
    columns = parquet_file.schema_arrow.names
    selected = [feature for feature in target_features if feature in columns]

    counts = {feature: 0 for feature in target_features}
    counts.update(_non_null_counts(parquet_file, selected, columns, use_threads))

    coverage_gaps: dict[str, dict[str, int | str]] = {}
    source_features = {
        feature: feature.removesuffix("_v2")
        for feature in selected
        if feature.endswith("_v2") and feature.removesuffix("_v2") in columns
    }
    pairs_requiring_row_read = {
        feature: source_feature
        for feature, source_feature in source_features.items()
        if _may_have_row_level_gap(parquet_file, source_feature, feature, columns)
    }
    if pairs_requiring_row_read:
        timestamp_column = "recorded_at" if "recorded_at" in columns else None
        coverage_columns = list(
            dict.fromkeys(
                [
                    *(pairs_requiring_row_read.values()),
                    *pairs_requiring_row_read.keys(),
                    *([timestamp_column] if timestamp_column else []),
                ]
            )
        )
        # A seekable AzureBlobRangeReader makes this a set of HTTP range reads for
        # just the v1/v2/timestamp column chunks. It is intentionally single-threaded
        # because the Python file object owns one seek position.
        table = parquet_file.read(columns=coverage_columns, use_threads=use_threads)
        for feature, source_feature in pairs_requiring_row_read.items():
            missing_mask = pc.and_(
                pc.is_valid(table[source_feature]),
                pc.is_null(table[feature]),
            )
            missing_rows = int(pc.sum(missing_mask).as_py() or 0)
            if not missing_rows:
                continue
            gap: dict[str, int | str] = {"missing_rows": missing_rows}
            if timestamp_column:
                missing_timestamps = pc.filter(table[timestamp_column], missing_mask)
                bounds = pc.min_max(missing_timestamps).as_py()
                if bounds and bounds.get("min") is not None:
                    gap["first_missing_at"] = _timestamp_text(bounds["min"])
                    gap["last_missing_at"] = _timestamp_text(bounds["max"])
            coverage_gaps[feature] = gap

    return ParquetFeatureSummary(
        row_count=row_count,
        columns=columns,
        feature_non_null_counts=counts,
        feature_coverage_gaps=coverage_gaps,
    )


def _non_null_counts(parquet_file, features: list[str], columns: list[str], use_threads: bool) -> dict[str, int]:
    """Read null counts from the footer, fetching a column only when stats are absent."""
    counts: dict[str, int] = {}
    needs_column_read: list[str] = []
    for feature in features:
        position = columns.index(feature)
        non_null = 0
        for row_group_index in range(parquet_file.metadata.num_row_groups):
            column = parquet_file.metadata.row_group(row_group_index).column(position)
            statistics = column.statistics
            if statistics is None or statistics.null_count is None:
                needs_column_read.append(feature)
                break
            non_null += column.num_values - statistics.null_count
        else:
            counts[feature] = non_null

    if needs_column_read:
        table = parquet_file.read(columns=needs_column_read, use_threads=use_threads)
        for feature in needs_column_read:
            counts[feature] = table[feature].length() - table[feature].null_count
    return counts


def _may_have_row_level_gap(parquet_file, source: str, target: str, columns: list[str]) -> bool:
    """Return false only when footer statistics prove a gap is impossible.

    A row group cannot contain ``source != null AND target == null`` when either
    the source is entirely null or the target is entirely non-null. Any missing
    statistics or mixed-null row group keeps the exact column-level check enabled.
    """
    source_position = columns.index(source)
    target_position = columns.index(target)
    for row_group_index in range(parquet_file.metadata.num_row_groups):
        row_group = parquet_file.metadata.row_group(row_group_index)
        source_column = row_group.column(source_position)
        target_column = row_group.column(target_position)
        source_stats = source_column.statistics
        target_stats = target_column.statistics
        if (
            source_stats is None
            or source_stats.null_count is None
            or target_stats is None
            or target_stats.null_count is None
        ):
            return True
        source_non_null = source_column.num_values - source_stats.null_count
        if source_non_null > 0 and target_stats.null_count > 0:
            return True
    return False


def _timestamp_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)
