from __future__ import annotations

from dataclasses import dataclass

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
        cfg = _create_config(Config, self.profile)
        with dbsql.connect(
            server_hostname=cfg.host.replace("https://", ""),
            http_path=f"/sql/1.0/warehouses/{self.warehouse_id}",
            credentials_provider=lambda: cfg.authenticate,
        ) as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()
            cursor.close()

        return {(row[0], int(row[1]), int(row[2])): int(row[3]) for row in rows}


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _create_config(config_class, profile: str, mode: str | None = None):
    """Use workspace/app credentials in Databricks and the local profile elsewhere."""
    runtime_mode = mode or resolve_runtime()
    return config_class() if runtime_mode == "databricks" else config_class(profile=profile)
