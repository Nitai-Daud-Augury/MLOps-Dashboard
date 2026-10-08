from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable

from .models import MonthPartition
from .runtime_mode import resolve_runtime


@dataclass(frozen=True)
class DatabricksSilverProvider:
    table: str
    profile: str
    warehouse_id: str
    enabled: bool = False

    def row_counts(
        self,
        machine_ids: list[str],
        partitions: list[MonthPartition],
    ) -> dict[tuple[str, int, int], int]:
        if not self.enabled or not machine_ids:
            return {}

        try:
            from databricks import sql as dbsql
            from databricks.sdk.core import Config
        except ImportError as exc:
            raise RuntimeError("Databricks SQL dependencies are missing.") from exc

        years_months = sorted({(partition.year, partition.month) for partition in partitions})
        machine_values = ", ".join(_quote(machine_id) for machine_id in machine_ids)
        date_predicate = " OR ".join(
            f"(year(recorded_at) = {year} AND month(recorded_at) = {month})"
            for year, month in years_months
        )
        query = f"""
            SELECT machine_id, year(recorded_at) AS year, month(recorded_at) AS month, COUNT(*) AS rows
            FROM {self.table}
            WHERE machine_id IN ({machine_values})
              AND ({date_predicate})
            GROUP BY machine_id, year(recorded_at), month(recorded_at)
        """

        # Databricks Apps supplies host and credentials through its runtime
        # environment; selecting a developer's local profile is both invalid
        # and unsafe there. Keep the profile-based flow for local development.
        cfg = shared_config(Config, self.profile)
        with dbsql.connect(
            server_hostname=cfg.host.replace("https://", ""),
            http_path=f"/sql/1.0/warehouses/{self.warehouse_id}",
            credentials_provider=serialized_credentials_provider(cfg),
        ) as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            cursor.close()

        return {(row[0], int(row[1]), int(row[2])): int(row[3]) for row in rows}


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _create_config(config_class, profile: str | None, mode: str | None = None):
    """Build a databricks-sdk ``Config`` without assuming a CLI profile.

    * Databricks Apps (runtime ``databricks``): always ``Config()``. The app's
      service principal is picked up from the injected ``DATABRICKS_HOST`` /
      ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET`` (OAuth M2M); a
      developer profile is never used there, even if one is set.
    * Elsewhere: ``Config(profile=...)`` only when a profile was explicitly
      configured (``DATABRICKS_PROFILE``); otherwise the SDK default auth chain
      (env vars, then ``~/.databrickscfg`` DEFAULT, ...).
    """
    runtime_mode = mode or resolve_runtime()
    if runtime_mode == "databricks":
        return config_class()
    selected = (profile or "").strip()
    return config_class(profile=selected) if selected else config_class()


def auth_description(profile: str | None, mode: str | None = None) -> str:
    """Non-secret label for logs: which auth path ``_create_config`` takes."""
    runtime_mode = mode or resolve_runtime()
    if runtime_mode == "databricks":
        return "auth=databricks-apps service principal (sdk default, env)"
    selected = (profile or "").strip()
    return f"profile={selected}" if selected else "auth=sdk default chain"


# One Config per (class, runtime, profile) so parallel SQL connections share a
# single token cache, and one lock around token acquisition so they never
# refresh concurrently (concurrent databricks-cli refreshes race on the token
# cache: "forced token refresh: cache update: exit status 45").
_CONFIG_CACHE: dict[tuple[Any, str, str], Any] = {}
_CONFIG_CACHE_LOCK = threading.Lock()
_AUTH_LOCK = threading.Lock()


def shared_config(config_class, profile: str | None, mode: str | None = None, *, factory: Callable[..., Any] | None = None):
    runtime_mode = mode or resolve_runtime()
    key = (config_class, runtime_mode, (profile or "").strip())
    with _CONFIG_CACHE_LOCK:
        cfg = _CONFIG_CACHE.get(key)
        if cfg is None:
            cfg = (factory or _create_config)(config_class, profile)
            _CONFIG_CACHE[key] = cfg
        return cfg


def clear_config_cache() -> None:
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE.clear()


def serialized_credentials_provider(cfg) -> Callable[[], Callable[[], dict[str, str]]]:
    """databricks-sql-connector ``credentials_provider`` with serialized auth.

    The connector calls the returned header factory for each request; the SDK
    caches/refreshes the token inside ``cfg``. The lock makes refreshes one at
    a time across all connections in this process.
    """

    def header_factory() -> dict[str, str]:
        with _AUTH_LOCK:
            return cfg.authenticate()

    return lambda: header_factory
