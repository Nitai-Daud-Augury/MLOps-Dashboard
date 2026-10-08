"""Databricks bronze machines_raw lifecycle provider for scan enrichment.

Queries ``dih_prod.bronze_augury_mh_mongodb.machines_raw`` (override via
``DATABRICKS_MACHINES_RAW_TABLE``, with legacy ``DATABRICKS_EQUIPMENT_TABLE``
fallback) for the curated scan cohort only.

Append-only source: latest snapshot per machine is
``max(ingestion_timestamp)`` via ``QUALIFY ROW_NUMBER() ... = 1``.

Installation timestamps prefer ``raw_json.created_at``. ``firstRecorded.timestamp``
is a go-live / first-sensor proxy and is used only when ``created_at`` is missing.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import threading
import time
from typing import Any, Callable, Sequence, TypeVar

from .inventory_models import MachineFacets, MachinePage, MachineRecord, MachineSearchQuery
from . import data_source_log as ds_log
from .inventory_provider import _record
from .silver import _create_config, _quote, auth_description, serialized_credentials_provider


_DEFAULT_MACHINES_RAW_TABLE = "dih_prod.bronze_augury_mh_mongodb.machines_raw"
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,2}$")
_T = TypeVar("_T")

# Keep lifecycle enrichment off the scan critical path when the warehouse is
# cold or auth stalls. Scanner already fail-opens on exceptions.
#
# Cold SQL warehouses regularly need ~2 minutes for the first statement; a 45s
# budget fail-opens before metadata arrives. Use a longer first-query / retry
# budget, then a tighter steady-state timeout once the warehouse is warm.
_DEFAULT_TIMEOUT_SECONDS = 45.0
_DEFAULT_COLD_TIMEOUT_SECONDS = 180.0


def _timeout_seconds() -> float:
    raw = (os.getenv("LIFECYCLE_DATABRICKS_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_TIMEOUT_SECONDS


def _cold_timeout_seconds(steady: float | None = None) -> float:
    """Timeout for first SQL / post-timeout retry; always >= steady timeout."""
    steady_value = _timeout_seconds() if steady is None else float(steady)
    raw = (os.getenv("LIFECYCLE_DATABRICKS_COLD_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        cold = _DEFAULT_COLD_TIMEOUT_SECONDS
    else:
        try:
            cold = float(raw)
        except ValueError:
            cold = _DEFAULT_COLD_TIMEOUT_SECONDS
        if cold <= 0:
            cold = _DEFAULT_COLD_TIMEOUT_SECONDS
    return max(cold, steady_value)


def _call_with_timeout(func: Callable[[], _T], timeout_seconds: float, *, label: str) -> _T:
    """Run ``func`` on a daemon thread; raise TimeoutError on deadline.

    Daemon workers are required so a hung Databricks auth/socket cannot keep the
    API process (or pytest) alive after the caller has already fail-opened.
    """
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = func()
        except Exception as exc:  # noqa: BLE001 - resurfaced to caller below
            box["error"] = exc

    thread = threading.Thread(
        target=runner,
        name=f"lifecycle-dbx-{label}",
        daemon=True,
    )
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TimeoutError(
            f"Databricks lifecycle {label} timed out after {timeout_seconds:g}s"
        )
    if "error" in box:
        raise box["error"]
    return box["value"]


class DatabricksLifecycleProvider:
    """Read-only lifecycle enricher backed by bronze Mongo machines_raw."""

    def __init__(
        self,
        *,
        table: str,
        profile: str,
        warehouse_id: str,
        connect=None,
        timeout_seconds: float | None = None,
        cold_timeout_seconds: float | None = None,
        config: Any = None,
    ) -> None:
        self.table = _validate_table(table)
        self.profile = (profile or "").strip() or None
        self.warehouse_id = warehouse_id
        self._connect = connect
        # One SDK Config (token cache) shared by every statement this provider
        # runs, including the concurrent feature_store cross-check queries.
        self._config = config
        self._config_lock = threading.Lock()
        self.timeout_seconds = (
            float(timeout_seconds) if timeout_seconds is not None else _timeout_seconds()
        )
        self.cold_timeout_seconds = (
            float(cold_timeout_seconds)
            if cold_timeout_seconds is not None
            else _cold_timeout_seconds(self.timeout_seconds)
        )
        if self.cold_timeout_seconds < self.timeout_seconds:
            self.cold_timeout_seconds = self.timeout_seconds
        # First real SQL against a stopped/cold warehouse is slow; after success
        # subsequent statements use the tighter steady timeout.
        self._sql_warm = False
        self._version = datetime.now(timezone.utc).isoformat()
        # Attempt notes ("cold 180s ok 12.3s") for the most recent get_many,
        # surfaced in the scanner's per-fetch [data-source] line.
        self.last_fetch_attempts: list[str] = []

    def get_version(self) -> str:
        return self._version

    def get_many(self, machine_ids: Sequence[str]) -> list[MachineRecord]:
        ids = [str(value).strip() for value in machine_ids if str(value).strip()]
        if not ids:
            return []
        rows: list[dict[str, Any]] = []
        self.last_fetch_attempts = []
        for chunk in _chunks(ids, 200):
            rows.extend(self._query_chunk(chunk))
        return [_record(_normalize_machines_raw_row(row), self._version) for row in rows]

    def search(self, query: MachineSearchQuery) -> MachinePage:
        # Lifecycle enrichment uses get_many only; pagination is unused.
        return MachinePage([], None, self._version, 0)

    def get_facets(self, query: MachineSearchQuery) -> MachineFacets:
        return MachineFacets()

    def _query_chunk(self, machine_ids: Sequence[str]) -> list[dict[str, Any]]:
        values = ", ".join(_quote(machine_id) for machine_id in machine_ids)
        # Databricks JSON path on the raw_json STRING column. QUALIFY keeps the
        # latest append-only snapshot per machine id. Colon syntax is preferred;
        # callers that need parse_json can swap the projection in a follow-up.
        # Prefer filtering via raw_json:_id (bronze layout). Timeouts in
        # `_execute` keep a cold warehouse / slow QUALIFY from pinning the scan.
        query = f"""
            SELECT
              raw_json:_id::string AS machine_id,
              raw_json:name::string AS machine_name,
              raw_json:containedIn:company:name::string AS company,
              raw_json:status::string AS status,
              raw_json:baseline:status::string AS baseline_status,
              raw_json:baseline:detailedStatus::string AS detailed_status,
              raw_json:baseline:detailedStatusChangedAt::string AS status_changed_at,
              raw_json:firstRecorded:timestamp::string AS first_recorded,
              raw_json:lastRecorded:timestamp::string AS last_recorded,
              raw_json:created_at::string AS created_at,
              raw_json:updated_at::string AS updated_at,
              CAST(raw_json:tags AS STRING) AS tags_json,
              CAST(raw_json:containedIn AS STRING) AS contained_in_json
            FROM {self.table}
            WHERE raw_json:_id::string IN ({values})
            QUALIFY ROW_NUMBER() OVER (
              PARTITION BY raw_json:_id::string
              ORDER BY ingestion_timestamp DESC
            ) = 1
        """
        columns, fetched = self._execute(query, label="lifecycle machines_raw")
        return [dict(zip(columns, row)) for row in fetched]

    def _execute(self, query: str, *, label: str = "sql") -> tuple[list[str], list[tuple[Any, ...]]]:
        if self._connect is not None:
            return self._connect(query)

        # Prefer the cold budget until the warehouse has answered once. If a
        # warm attempt still times out (warehouse slept again), retry once with
        # the cold budget — then fail-open for the scanner.
        attempts: list[float] = []
        if self._sql_warm:
            attempts.append(self.timeout_seconds)
            if self.cold_timeout_seconds > self.timeout_seconds:
                attempts.append(self.cold_timeout_seconds)
        else:
            attempts.append(self.cold_timeout_seconds)

        last_error: Exception | None = None
        was_warm = self._sql_warm
        notes: list[str] = []
        for index, budget in enumerate(attempts):
            kind = "warm" if (was_warm and index == 0) else "cold"
            attempt = f"attempt {index + 1}/{len(attempts)} ({kind}{' retry' if index else ''}, budget {budget:g}s)"
            started = time.monotonic()
            try:
                result = _call_with_timeout(
                    lambda budget=budget: self._execute_databricks(query, timeout_seconds=budget),
                    budget,
                    label="SQL execute",
                )
                elapsed = time.monotonic() - started
                self._sql_warm = True
                notes.append(f"{kind}{' retry' if index else ''} {budget:g}s ok {elapsed:.1f}s")
                if label.startswith("lifecycle"):
                    self.last_fetch_attempts.extend(notes)
                ds_log.info(
                    f"databricks sql [{label}]: {attempt} ok in {elapsed:.1f}s rows={len(result[1])} "
                    f"warehouse={self.warehouse_id}"
                )
                return result
            except TimeoutError as exc:
                last_error = exc
                notes.append(f"{kind}{' retry' if index else ''} {budget:g}s timeout")
                if index + 1 >= len(attempts):
                    if label.startswith("lifecycle"):
                        self.last_fetch_attempts.extend(notes)
                    ds_log.warning(
                        f"databricks sql [{label}]: {attempt} timed out after {budget:g}s; no attempts left "
                        f"(warehouse={self.warehouse_id}) -> caller fails open"
                    )
                    raise
                # One cold retry after a warm-path timeout.
                ds_log.warning(
                    f"databricks sql [{label}]: {attempt} timed out after {budget:g}s "
                    f"-> retrying once with cold budget {attempts[index + 1]:g}s"
                )
                continue
            except Exception as exc:
                notes.append(f"{kind} {budget:g}s error")
                if label.startswith("lifecycle"):
                    self.last_fetch_attempts.extend(notes)
                ds_log.warning(
                    f"databricks sql [{label}]: {attempt} failed after {time.monotonic() - started:.1f}s: "
                    f"{ds_log.error_text(exc)} (warehouse={self.warehouse_id}) -> caller fails open"
                )
                raise
        assert last_error is not None
        raise last_error

    @property
    def auth_description(self) -> str:
        return auth_description(self.profile)

    def _sdk_config(self, config_class):
        with self._config_lock:
            if self._config is None:
                self._config = _create_config(config_class, self.profile)
            return self._config

    def _execute_databricks(
        self,
        query: str,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        from databricks import sql as dbsql
        from databricks.sdk.core import Config

        cfg = self._sdk_config(Config)
        hostname = cfg.host.replace("https://", "").replace("http://", "")
        budget = float(timeout_seconds) if timeout_seconds is not None else self.timeout_seconds
        # Bound socket wait to the call budget so cold start can finish, but a
        # truly hung socket still cannot pin the scan forever.
        socket_timeout = max(5, int(budget))
        with dbsql.connect(
            server_hostname=hostname,
            http_path=f"/sql/1.0/warehouses/{self.warehouse_id}",
            credentials_provider=serialized_credentials_provider(cfg),
            _socket_timeout=socket_timeout,
        ) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(query)
                rows = cursor.fetchall()
                description = cursor.description or ()
                columns = [col[0] for col in description]
            finally:
                cursor.close()
        return columns, [tuple(row) for row in rows]


def build_databricks_lifecycle_provider() -> tuple[DatabricksLifecycleProvider | None, str]:
    warehouse_id = (os.getenv("DATABRICKS_WAREHOUSE_ID") or "").strip()
    if not warehouse_id:
        return None, "not_configured"
    table = _resolve_machines_table()
    # Profile only when explicitly configured; otherwise the SDK default chain
    # (in Databricks Apps: the app service principal from injected env).
    profile = (os.getenv("DATABRICKS_PROFILE") or "").strip() or None
    try:
        _validate_table(table)
    except ValueError:
        return None, "not_configured"
    try:
        from databricks import sql as dbsql  # noqa: F401
        from databricks.sdk.core import Config
    except ModuleNotFoundError:
        return None, "dependency_missing"
    try:
        timeout = _timeout_seconds()
        cfg = _call_with_timeout(
            lambda: _create_config(Config, profile),
            timeout,
            label="auth/config",
        )
        if not getattr(cfg, "host", None):
            return None, "not_configured"
        return (
            DatabricksLifecycleProvider(
                table=table,
                profile=profile,
                warehouse_id=warehouse_id,
                timeout_seconds=timeout,
                cold_timeout_seconds=_cold_timeout_seconds(timeout),
                config=cfg,
            ),
            "healthy",
        )
    except TimeoutError:
        ds_log.warning(f"databricks lifecycle provider: auth/config timed out ({auth_description(profile)}) -> status unreachable")
        return None, "unreachable"
    except Exception as exc:
        text = str(exc).lower()
        status = "unauthorized" if ("auth" in text or "unauthorized" in text or "permission" in text) else "unreachable"
        ds_log.warning(
            f"databricks lifecycle provider: auth/config failed ({auth_description(profile)}): {ds_log.error_text(exc)} -> status {status}"
        )
        return None, status


def _resolve_machines_table() -> str:
    """Prefer MACHINES_RAW_TABLE; accept legacy EQUIPMENT_TABLE; else bronze default."""
    for key in ("DATABRICKS_MACHINES_RAW_TABLE", "DATABRICKS_EQUIPMENT_TABLE"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return _DEFAULT_MACHINES_RAW_TABLE


def _normalize_machines_raw_row(row: dict[str, Any]) -> dict[str, Any]:
    machine_id = str(row.get("machine_id") or "")
    name = row.get("machine_name") or machine_id
    tags = _parse_tags(row.get("tags_json"))
    status = _resolve_status(row)
    archived = status.lower() in {
        "archived",
        "deactivated",
        "inactive",
        "disabled",
    }
    hierarchy = _hierarchy_from_contained_in(row.get("contained_in_json"))
    company = row.get("company")
    if company and "organizationName" not in hierarchy:
        hierarchy["organizationName"] = company
    installation_at = _installation_from_created_at(
        row.get("created_at"),
        row.get("first_recorded"),
    )
    last_recorded = _optional_iso(row.get("last_recorded"))
    updated_at = row.get("updated_at") or row.get("status_changed_at")
    return {
        "_id": machine_id,
        "machine_id": machine_id,
        "name": name,
        "display_name": name,
        "status": status,
        "archived": archived,
        "tags": tags,
        "endpoints": [],
        "installation_date": installation_at,
        "first_recorded_at": _optional_iso(row.get("first_recorded")) or _epoch_to_iso(row.get("first_recorded")),
        "last_recorded_at": last_recorded,
        "updated_at": _optional_iso(updated_at),
        **hierarchy,
    }


def _resolve_status(row: dict[str, Any]) -> str:
    """Prefer raw status; fall back to baseline detailedStatus / status as enrichment."""
    for key in ("status", "detailed_status", "baseline_status"):
        value = row.get(key)
        if value is None or value == "":
            continue
        text = str(value).strip()
        if text:
            return text
    return "unknown"


def _installation_from_created_at(created_at: Any, first_recorded: Any) -> str | None:
    """Map bronze created_at to installation_at; firstRecorded only as fallback.

    Prefer ``raw_json.created_at`` as the install / configuration-created boundary.
    ``firstRecorded.timestamp`` is a go-live / first-sensor proxy and is used only
    when ``created_at`` is absent. This differs from Mongo endpoint
    ``installation_date`` and from the retired Silver MC ``createdAt`` path.
    """
    primary = _optional_iso(created_at) or _epoch_to_iso(created_at)
    if primary:
        return primary
    return _optional_iso(first_recorded) or _epoch_to_iso(first_recorded)


def _epoch_to_iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    # Accept epoch milliseconds or seconds.
    if raw >= 1_000_000_000_000:
        seconds = raw / 1000.0
    elif raw >= 1_000_000_000:
        seconds = raw
    else:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _parse_tags(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [part.strip() for part in text.split(",") if part.strip()]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item is not None]
        if parsed is None:
            return []
        return [str(parsed)]
    return [str(value)]


def _hierarchy_from_contained_in(value: Any) -> dict[str, Any]:
    document = _parse_json_object(value)
    if not document:
        return {}
    company = document.get("company") or {}
    contained_type = str(document.get("type", "")).lower()
    is_site = contained_type in {"facility", "site"}
    result: dict[str, Any] = {}
    if is_site and document.get("_id") is not None:
        result["siteId"] = str(document.get("_id"))
        result["siteName"] = document.get("name")
    if isinstance(company, dict) and company.get("_id") is not None:
        result["organizationId"] = str(company.get("_id"))
        result["organizationName"] = company.get("name")
    elif isinstance(company, dict) and company.get("name"):
        result["organizationName"] = company.get("name")
    return result


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text or text.lower() == "null":
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_iso(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        text = str(isoformat())
        return text.replace("+00:00", "Z") if text.endswith("+00:00") else text
    text = str(value).strip()
    return text or None


def _validate_table(name: str) -> str:
    if not _TABLE_RE.fullmatch(name):
        raise ValueError(f"Invalid Databricks machines_raw table identifier: {name!r}")
    return name


def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]
