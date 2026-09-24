from backfill_dashboard.models import MonthPartition


def test_partition_blob_suffix_uses_feature_store_layout():
    partition = MonthPartition(index=17, year=2026, month=1)

    assert partition.quarter == "2026Q1"
    assert (
        partition.blob_path_suffix
        == "quarter=2026Q1/month=1/partition_version=last/part-0.parquet"
    )

