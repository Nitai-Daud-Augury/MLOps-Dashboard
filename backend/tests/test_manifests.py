from __future__ import annotations

import pytest

from backfill_dashboard.credentials import ManifestCredentialResolver
from backfill_dashboard.manifests import (
    BackfillManifestWriter,
    MachineManifestRequest,
    MultiMachineManifestRequest,
    machine_manifest_path,
    multi_machine_manifest_path,
    normalized_timestamp,
    orchestrator_month_index,
    orchestrator_month_windows,
    validate_timestamp,
    validate_machine_id,
    validate_machine_ids,
    validate_manifest_path,
)
from datetime import datetime, timezone


def test_machine_manifest_path_is_safe_relative_parquet_path():
    path = machine_manifest_path(
        MachineManifestRequest(
            machine_id="683ec59079fecb5a5a240478",
            since="2026-02-01T00:00:00",
            until="2026-03-01T00:00:00",
        )
    )

    assert path.endswith("/683ec59079fecb5a5a240478_20260201_20260301.parquet")
    assert path.startswith("2026/")


def test_machine_manifest_requires_until_after_since():
    with pytest.raises(ValueError, match="until must be after since"):
        machine_manifest_path(
            MachineManifestRequest(
                machine_id="683ec59079fecb5a5a240478",
                since="2026-03-01T00:00:00",
                until="2026-02-01T00:00:00",
            )
        )


def test_machine_id_and_manifest_path_validation_rejects_unsafe_values():
    with pytest.raises(ValueError):
        validate_machine_id("../../bad")

    with pytest.raises(ValueError):
        validate_manifest_path("../manifest.parquet")


def test_multi_machine_manifest_deduplicates_ids_and_uses_shared_time_range():
    request = MultiMachineManifestRequest(
        machine_ids=["683ec59079fecb5a5a240478", "683ec59079fecb5a5a240478", "68594353ce7a21678636844d"],
        since="2026-04-01T00:00:00",
        until="2026-05-01T00:00:00",
    )

    path = multi_machine_manifest_path(request)

    assert validate_machine_ids(request.machine_ids) == [
        "683ec59079fecb5a5a240478",
        "68594353ce7a21678636844d",
    ]
    assert path.endswith("/2_machines_20260401_20260501.parquet")


def test_date_only_timestamp_is_written_at_midnight():
    parsed = validate_timestamp("2026-04-01", field_name="since")

    assert normalized_timestamp(parsed) == "2026/04/01/00"


def test_orchestrator_month_windows_split_a_range_by_parent_month_index():
    windows = orchestrator_month_windows(
        validate_timestamp("2025-01-01", field_name="since"),
        validate_timestamp("2025-03-01", field_name="until"),
    )

    assert [(index, year, month) for index, year, month, _, _ in windows] == [
        (5, 2025, 1),
        (6, 2025, 2),
    ]
    assert normalized_timestamp(windows[0][3]) == "2025/01/01/00"


def test_orchestrator_month_index_is_anchored_at_august_2024():
    assert orchestrator_month_index(2026, 1) == 17
    assert orchestrator_month_index(2026, 3) == 19


def test_manifest_credential_prefers_explicit_environment_key():
    resolver = ManifestCredentialResolver(
        environment={"FST_BACKFILL_ACCOUNT_KEY": "test-key"},
        key_vault_loader=lambda: pytest.fail("Key Vault should not be called"),
    )

    credential = resolver.resolve()

    assert credential.kind == "account_key"
    assert credential.source == "environment"
    assert credential.value == "test-key"


def test_manifest_credential_loads_the_production_backfill_key_from_key_vault():
    resolver = ManifestCredentialResolver(environment={}, key_vault_loader=lambda: "key-vault-key")

    assert resolver.readiness() == {
        "ready": True,
        "credential_source": "key_vault",
        "credential_kind": "account_key",
    }


def test_manifest_credential_fails_without_explicit_or_key_vault_access():
    resolver = ManifestCredentialResolver(environment={}, key_vault_loader=lambda: "")

    with pytest.raises(RuntimeError, match="empty backfill account key"):
        resolver.resolve()


class _FakeBlob:
    def __init__(self, path, uploads): self.path, self.uploads = path, uploads
    def upload_blob(self, content, *, overwrite):
        self.uploads.append((self.path, content, overwrite))


class _FakeContainer:
    def __init__(self): self.uploads = []
    def get_blob_client(self, path): return _FakeBlob(path, self.uploads)


def _daily_writer(monkeypatch):
    container = _FakeContainer()
    writer = BackfillManifestWriter(account_name="test", container_name="test")
    monkeypatch.setattr(writer, "_container", lambda: container)
    return writer, container


def test_daily_gap_writer_writes_one_machine_day_row_per_create_only_file(monkeypatch):
    pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq
    import io
    writer, container = _daily_writer(monkeypatch)
    result = writer.write_daily_gap_manifests(
        machine_id="683ec59079fecb5a5a240478", month_indices=[18],
        now=datetime(2026, 3, 3, 12, tzinfo=timezone.utc),
    )
    assert result.month_count == 1 and result.day_count == 28
    assert len(container.uploads) == 28
    assert all(overwrite is False for _, _, overwrite in container.uploads)
    for path, data, _ in container.uploads:
        table = pq.read_table(io.BytesIO(data)).to_pydict()
        assert table["machine_id"] == ["683ec59079fecb5a5a240478"]
        assert table["since"][0].endswith("/00")
        assert table["until"][0].endswith("/00")
        assert path.endswith(table["since"][0][:10].replace("/", "") + ".parquet")


def test_daily_gap_writer_handles_leap_day_and_month_boundary(monkeypatch):
    writer, _ = _daily_writer(monkeypatch)
    result = writer.write_daily_gap_manifests(
        machine_id="683ec59079fecb5a5a240478", month_indices=[19, 20],
        now=datetime(2026, 4, 2, tzinfo=timezone.utc),
    )
    # March 2026 + April 1, capped at yesterday.
    assert result.day_count == 32
    assert result.manifests[-1]["since"] == "2026/04/01/00"
    assert result.manifests[-1]["until"] == "2026/04/02/00"


def test_daily_gap_writer_includes_leap_day(monkeypatch):
    writer, _ = _daily_writer(monkeypatch)
    result = writer.write_daily_gap_manifests(
        machine_id="683ec59079fecb5a5a240478",
        # August 2024 is index 0, so February 2028 is index 42.
        month_indices=[42],
        now=datetime(2028, 3, 1, tzinfo=timezone.utc),
    )
    assert result.day_count == 29
    assert result.manifests[28]["since"] == "2028/02/29/00"


def test_daily_gap_writer_reports_partial_upload(monkeypatch):
    writer, container = _daily_writer(monkeypatch)
    uploads = 0
    original = container.get_blob_client
    class FailingBlob:
        def __init__(self, blob): self.blob = blob
        def upload_blob(self, content, *, overwrite):
            nonlocal uploads
            uploads += 1
            if uploads == 2: raise RuntimeError("upload failed")
            self.blob.upload_blob(content, overwrite=overwrite)
    monkeypatch.setattr(container, "get_blob_client", lambda path: FailingBlob(original(path)))
    with pytest.raises(RuntimeError, match="failed after 1 of 31 files"):
        writer.write_daily_gap_manifests(machine_id="683ec59079fecb5a5a240478", month_indices=[19], now=datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert len(container.uploads) == 1


def test_daily_gap_writer_validates_day_limit_before_first_upload(monkeypatch):
    writer, container = _daily_writer(monkeypatch)
    with pytest.raises(ValueError, match="maximum 93 completed days"):
        writer.write_daily_gap_manifests(
            machine_id="683ec59079fecb5a5a240478",
            month_indices=[orchestrator_month_index(2026, month) for month in (1, 2, 3, 4)],
            now=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
    assert container.uploads == []


def test_daily_gap_writer_current_month_stops_before_today_and_prefix_is_unique(monkeypatch):
    writer, _ = _daily_writer(monkeypatch)
    now = datetime(2026, 3, 2, 10, tzinfo=timezone.utc)
    index = orchestrator_month_index(2026, 3)
    a = writer.write_daily_gap_manifests(machine_id="683ec59079fecb5a5a240478", month_indices=[index], manifest_prefix="review", now=now)
    b = writer.write_daily_gap_manifests(machine_id="683ec59079fecb5a5a240478", month_indices=[index], manifest_prefix="review", now=now)
    assert a.day_count == 1
    assert a.manifest_prefix != b.manifest_prefix


def _orch_writer(monkeypatch):
    container = _FakeContainer()
    writer = BackfillManifestWriter(account_name="test", container_name="test")
    monkeypatch.setattr(writer, "_container", lambda: container)
    return writer, container


def test_orchestrated_writer_honors_exact_range(monkeypatch):
    pytest.importorskip("pyarrow")
    import io
    import pyarrow.parquet as pq
    writer, container = _orch_writer(monkeypatch)
    result = writer.write_orchestrated_monthly_manifests(
        machine_ids=["6817571193e37ef05fffcd1c"],
        since="2026-03-01T00:00:00Z",
        until="2026-03-15T00:00:00Z",
        manifest_prefix="proof/x",
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    assert result.manifest_count == 1
    assert len(container.uploads) == 1
    path, data, _ = container.uploads[0]
    assert path.endswith("/month_19_2026_03.parquet")
    table = pq.read_table(io.BytesIO(data)).to_pydict()
    assert table == {
        "machine_id": ["6817571193e37ef05fffcd1c"],
        "since": ["2026/03/01/00"],
        "until": ["2026/03/15/00"],
    }


def test_orchestrated_writer_range_with_month_indices_matches(monkeypatch):
    pytest.importorskip("pyarrow")
    import io
    import pyarrow.parquet as pq
    writer, container = _orch_writer(monkeypatch)
    result = writer.write_orchestrated_monthly_manifests(
        machine_ids=["6817571193e37ef05fffcd1c"],
        since="2026-03-01T00:00:00Z",
        until="2026-03-15T00:00:00Z",
        month_indices_by_machine={"6817571193e37ef05fffcd1c": [19]},
        manifest_prefix="proof/x",
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    assert result.manifest_count == 1
    table = pq.read_table(io.BytesIO(container.uploads[0][1])).to_pydict()
    assert table["since"] == ["2026/03/01/00"] and table["until"] == ["2026/03/15/00"]


def test_orchestrated_writer_month_filter_drops_outside_range(monkeypatch):
    writer, container = _orch_writer(monkeypatch)
    with pytest.raises(ValueError, match="No manifests matched"):
        writer.write_orchestrated_monthly_manifests(
            machine_ids=["6817571193e37ef05fffcd1c"],
            since="2026-03-01T00:00:00Z",
            until="2026-03-15T00:00:00Z",
            month_indices_by_machine={"6817571193e37ef05fffcd1c": [18]},
            manifest_prefix="proof/x",
            now=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
    assert container.uploads == []


def test_orchestrated_writer_indices_only_keeps_full_month(monkeypatch):
    pytest.importorskip("pyarrow")
    import io
    import pyarrow.parquet as pq
    writer, container = _orch_writer(monkeypatch)
    result = writer.write_orchestrated_monthly_manifests(
        machine_ids=["6817571193e37ef05fffcd1c"],
        since="",
        until="",
        month_indices_by_machine={"6817571193e37ef05fffcd1c": [19]},
        manifest_prefix="proof/x",
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    table = pq.read_table(io.BytesIO(container.uploads[0][1])).to_pydict()
    assert table["since"] == ["2026/03/01/00"] and table["until"] == ["2026/04/01/00"]


def test_orchestrated_writer_allows_day_split_when_sink_is_azure_fake(monkeypatch):
    """Writer-level gate removed; API readiness is the single gate for live triggers."""
    pytest.importorskip("pyarrow")
    writer, container = _orch_writer(monkeypatch)
    result = writer.write_orchestrated_monthly_manifests(
        machine_ids=["6817571193e37ef05fffcd1c"],
        since="2026-03-01T00:00:00Z",
        until="2026-03-15T00:00:00Z",
        split={"mode": "day"},
        manifest_prefix="proof/x",
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    assert result.manifest_count == 14
    assert len(container.uploads) == 14
    assert "part_000_" in container.uploads[0][0]


def test_orchestrated_writer_local_sink_allows_day_split(monkeypatch, tmp_path):
    pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq
    from backfill_dashboard.manifests import LocalDirectorySink, plan_orchestrated_manifests
    writer = BackfillManifestWriter(account_name="test", container_name="test")
    monkeypatch.setattr(writer, "_container", lambda: pytest.fail("LocalDirectorySink must not call _container"))
    sink = LocalDirectorySink(tmp_path)
    result = writer.write_orchestrated_monthly_manifests(
        machine_ids=["6817571193e37ef05fffcd1c"],
        since="2026-03-01T00:00:00Z",
        until="2026-03-15T00:00:00Z",
        split={"mode": "day"},
        manifest_prefix="proof/x",
        now=datetime(2026, 4, 1, tzinfo=timezone.utc),
        sink=sink,
    )
    assert result.manifest_count == 14
    files = sorted(tmp_path.rglob("*.parquet"))
    assert len(files) == 14
    assert "part_000_" in files[0].name
    table = pq.read_table(files[0]).to_pydict()
    assert set(table) == {"machine_id", "since", "until"}
    assert all(isinstance(v, str) for col in table.values() for v in col)
    assert plan_orchestrated_manifests


def test_render_manifest_table_schema():
    pytest.importorskip("pyarrow")
    from backfill_dashboard.manifests import PlannedManifest, render_manifest_table
    from backfill_dashboard.windows import ManifestWindow
    planned = PlannedManifest(
        machine_id="6817571193e37ef05fffcd1c",
        window=ManifestWindow(
            month_index=19, year=2026, month=3, chunk_index=0, chunk_count=1,
            since=datetime(2026, 3, 1, tzinfo=timezone.utc),
            until=datetime(2026, 3, 2, tzinfo=timezone.utc),
        ),
        manifest_path="x.parquet",
    )
    table = render_manifest_table(planned)
    assert table.column_names == ["machine_id", "since", "until"]
    assert table.to_pydict()["since"] == ["2026/03/01/00"]
