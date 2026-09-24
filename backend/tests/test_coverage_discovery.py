from __future__ import annotations

from datetime import datetime, timezone

from backfill_dashboard.config import Settings
from backfill_dashboard.months import orchestrator_month_index
from backfill_dashboard.scanner import BackfillScanner, _months_from_blob_names


def test_blob_partition_dates_ignore_virtual_directories_and_extract_months():
    blob_names = [
        "machine_id=one/quarter=2024Q4",
        "machine_id=one/quarter=2025Q3/month=8/partition_version=last",
        "machine_id=one/quarter=2025Q3/month=8/partition_version=last/part-0.parquet",
        "machine_id=one/quarter=2025Q4/month=12/partition_version=last/part-0.parquet",
    ]

    assert _months_from_blob_names(blob_names) == [(2025, 8), (2025, 12)]


def test_scan_partition_indexes_stay_anchored_to_orchestrator_months():
    """A later coverage start must not renumber months selected for backfill."""
    scanner = BackfillScanner(settings=Settings(), inventory=None, blob_store=None, silver_provider=None)

    partitions = scanner._coverage_partitions({"machine": (2025, 4)})
    by_month = {(partition.year, partition.month): partition.index for partition in partitions}

    assert by_month[(2026, 1)] == 17
    assert by_month[(2026, 3)] == 19
    now = datetime.now(timezone.utc)
    assert by_month[(now.year, now.month)] == orchestrator_month_index(now.year, now.month)


def test_installation_can_start_calendar_before_first_fst_partition():
    scanner = BackfillScanner(settings=Settings(), inventory=None, blob_store=None, silver_provider=None)

    partitions = scanner._coverage_partitions({"machine": (2026, 8)})

    assert (partitions[0].year, partitions[0].month) == (2026, 8)
