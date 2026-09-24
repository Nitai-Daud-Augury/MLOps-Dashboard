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
