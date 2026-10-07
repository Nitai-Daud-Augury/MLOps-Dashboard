from __future__ import annotations

import os
import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from dotenv import load_dotenv

from .months import orchestrator_months


EXPECTED_ULTRASONIC_V2_COLUMNS = [
    "ultrasonic_p2p_v2",
    "ultrasonic_rms_v2",
    "ultrasonic_mad_v2",
    "ultrasonic_hf_rms_v2",
    "ultrasonic_rms_30_35_v2",
    "ultrasonic_rms_35_40_v2",
    "ultrasonic_rms_40_45_v2",
    "ultrasonic_impact_energy_v2",
]

# Load only backend configuration. This runs before Settings' environment
# defaults are evaluated. The dashboard root is two levels above this file
# (`drift-dashboard/`); deployment defaults must not depend on sibling repos.
_dashboard_root = Path(__file__).resolve().parents[2]
load_dotenv(_dashboard_root / ".env")
load_dotenv(_dashboard_root / "backend" / ".env", override=False)


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _machine_ids_path() -> Path:
    configured = os.getenv("ULRPM_MACHINE_IDS_FILE")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else _dashboard_root / path
    return _dashboard_root / "data" / "unique_machine_ids.txt"


def _test_machine_ids_path() -> Path:
    configured = os.getenv("MLOPS_TEST_MACHINE_IDS_FILE")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else _dashboard_root / path
    return _dashboard_root / "data" / "test_machine_ids.txt"


def load_test_machine_ids(path: Path, additional_ids: str | None = None) -> frozenset[str]:
    """Load an explicit test-machine allowlist without consulting inventory services."""
    if not path.is_file():
        raise FileNotFoundError(f"Test machine registry not found: {path}")
    machine_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        normalized = value.lower()
        if not re.fullmatch(r"[0-9a-f]{24}", normalized):
            raise ValueError(f"Invalid test machine ID at {path}:{line_number}: {value!r}")
        machine_ids.add(normalized)
    for value in (additional_ids or "").split(","):
        value = value.strip()
        if not value:
            continue
        normalized = value.lower()
        if not re.fullmatch(r"[0-9a-f]{24}", normalized):
            raise ValueError(f"Invalid MLOPS_TEST_MACHINE_IDS entry: {value!r}")
        machine_ids.add(normalized)
    return frozenset(machine_ids)


@dataclass(frozen=True)
class Settings:
    fst_account: str = os.getenv("FST_PROD_ACCOUNT_NAME", "auguryprodfsthns")
    fst_container: str = os.getenv("FST_PROD_CONTAINER", "feature-store-container")
    target_features: list[str] = None  # type: ignore[assignment]
    target_months: list[tuple[int, int]] = None  # type: ignore[assignment]
    canonical_schema_path: Path = Path(
        os.getenv(
            "BACKFILL_CANONICAL_SCHEMA_PATH",
            Path(__file__).resolve().parent / "reference" / "full_fst_schema_v1.json",
        )
    )
    machine_ids_file: Path = _machine_ids_path()
    test_machine_ids_file: Path = field(default_factory=_test_machine_ids_path)
    test_machine_ids: frozenset[str] | None = None
    augury_machine_url_template: str = os.getenv(
        "AUGURY_MACHINE_URL_TEMPLATE",
        "https://app.augury.com/#/machine_health/machines/{machine_id}",
    )
    state_path: Path = Path(
        os.getenv("BACKFILL_DASHBOARD_STATE_PATH", "/tmp/ulrpm_backfill_dashboard_state.json")
    )
    scan_workers: int = int(os.getenv("BACKFILL_SCAN_WORKERS", "12"))
    silver_enabled: bool = os.getenv("DATABRICKS_SILVER_ENABLED", "0") == "1"
    silver_table: str = os.getenv("DATABRICKS_SILVER_TABLE", "dih_prod.silver_mh.features")
    databricks_profile: str = os.getenv("DATABRICKS_PROFILE", "Augury")
    warehouse_id: str = os.getenv("DATABRICKS_WAREHOUSE_ID", "6ed9ddd0b2661edc")
    no_data_before_year: int = int(os.getenv("ULRPM_NO_DATA_BEFORE_YEAR", "2025"))
    no_data_before_month: int = int(os.getenv("ULRPM_NO_DATA_BEFORE_MONTH", "5"))
    mongodb_database: str = os.getenv("MONGODB_DATABASE", "production")
    mongodb_collection: str = os.getenv("MONGODB_COLLECTION", "machines")
    control_plane_db_path: Path = Path(
        os.getenv("BACKFILL_CONTROL_PLANE_DB_PATH", "/tmp/backfill_control_plane.sqlite3")
    )
    dispatch_enabled: bool = os.getenv("BACKFILL_DISPATCH_ENABLED", "0") == "1"
    production_mode: bool = os.getenv("BACKFILL_PRODUCTION_MODE", "0") == "1"
    estimate_ttl_seconds: int = int(os.getenv("BACKFILL_ESTIMATE_TTL_SECONDS", "900"))
    scan_idle_timeout_seconds: int = int(os.getenv("BACKFILL_SCAN_IDLE_TIMEOUT_SECONDS", "600"))
    scan_heartbeat_interval_seconds: int = int(os.getenv("BACKFILL_SCAN_HEARTBEAT_SECONDS", "30"))

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_features",
            _csv_env("BACKFILL_TARGET_FEATURES", EXPECTED_ULTRASONIC_V2_COLUMNS),
        )
        object.__setattr__(self, "target_months", orchestrator_months())
        configured_ids = self.test_machine_ids
        if configured_ids is None:
            configured_ids = load_test_machine_ids(
                self.test_machine_ids_file,
                os.getenv("MLOPS_TEST_MACHINE_IDS"),
            )
        object.__setattr__(self, "test_machine_ids", frozenset(item.lower() for item in configured_ids))


def get_settings() -> Settings:
    return Settings()
