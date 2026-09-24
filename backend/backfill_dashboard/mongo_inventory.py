from __future__ import annotations

from datetime import datetime, timezone
import os
import re
from typing import Any, Sequence

from .inventory_models import MachineFacets, MachinePage, MachineRecord, MachineSearchQuery
from .inventory_provider import _counts, _record, decode_cursor, encode_cursor


class MongoMachineInventoryProvider:
    """Read-only, seek-paginated Mongo inventory adapter.

    The provider deliberately keeps the projection narrow. Production should
    normally put a synchronizer/read model in front of this adapter; it remains
    useful for local deployments and integration tests.
    """
    def __init__(self, client: Any, database: str, collection: str = "machines", configuration_collection: str = "machine_configurations", endpoint_collection: str = "endpoints") -> None:
        self.machines = client[database][collection]
        self.collection = client[database][configuration_collection]
        self.endpoints = client[database][endpoint_collection]
        self.machine_id_field = os.getenv("MONGODB_CONFIGURATION_MACHINE_ID_FIELD", "_id")
        self.endpoint_machine_field = os.getenv("MONGODB_ENDPOINT_MACHINE_ID_FIELD", "machine.id")
        self._version = datetime.now(timezone.utc).isoformat()

    def get_version(self) -> str:
        return self._version

    def list_machine_ids(self) -> list[str]:
        """Compatibility for legacy scan routes; new inventory APIs paginate."""
        return [str(row["_id"]) for row in self.collection.find({}, {"_id": 1})]

    def _filter(self, query: MachineSearchQuery) -> dict[str, Any]:
        # Keep the complete collection visible; archived/disabled machines are
        # shown as ineligible rather than silently disappearing.
        result: dict[str, Any] = {}
        if query.search:
            result["name"] = {"$regex": re.escape(query.search[:200]), "$options": "i"}
        return result

    @staticmethod
    def _projection() -> dict[str, int]:
        return {"_id": 1, "machineId": 1, "name": 1, "display_name": 1, "status": 1, "archived": 1, "tags": 1,
                "endpoints": 1, "components": 1, "siteId": 1, "organizationId": 1,
                "updated_at": 1, "updatedAt": 1, "lastRecorded": 1,
                "last_recorded_at": 1, "lastRecordedAt": 1}

    @staticmethod
    def _mongo_id(value: str) -> Any:
        try:
            from bson import ObjectId
            return ObjectId(value) if len(value) == 24 else value
        except (ImportError, TypeError):
            return value

    def _enrich(self, rows: list[dict[str, Any]]) -> list[MachineRecord]:
        if not rows:
            return []
        ids = [row.get(self.machine_id_field, row["_id"]) for row in rows]
        machines = {row["_id"]: row for row in self.machines.find({"_id": {"$in": ids}}, {"_id": 1, "name": 1, "display_name": 1, "tags": 1, "siteId": 1, "siteName": 1, "organizationId": 1, "organizationName": 1, "status": 1, "archived": 1, "updated_at": 1, "updatedAt": 1, "lastRecorded": 1, "last_recorded_at": 1, "lastRecordedAt": 1})}
        endpoint_map: dict[Any, list[dict[str, Any]]] = {value: [] for value in ids}
        endpoint_projection = {
            self.endpoint_machine_field: 1,
            "info": 1,
            "type": 1,
            "installation_date": 1,
            "installationDate": 1,
            "created_at": 1,
            "isInstalled": 1,
            "lastSample": 1,
        }
        for endpoint in self.endpoints.find(
            {self.endpoint_machine_field: {"$in": ids}}, endpoint_projection
        ):
            info = endpoint.get("info") or {}
            hardware = info.get("type") or info.get("partNumber") or endpoint.get("type")
            machine_id = _nested_value(endpoint, self.endpoint_machine_field)
            if isinstance(hardware, str) and machine_id in endpoint_map:
                endpoint_map[machine_id].append({
                    "type": hardware,
                    "installation_date": endpoint.get(
                        "installation_date", endpoint.get("installationDate")
                    ),
                    "created_at": endpoint.get("created_at"),
                    "is_installed": endpoint.get("isInstalled"),
                    "last_sample": endpoint.get("lastSample"),
                })
        normalized = []
        for row in rows:
            machine_id = row.get(self.machine_id_field, row["_id"])
            machine = machines.get(machine_id, {})
            # Joined endpoint documents are authoritative for lifecycle fields such
            # as installation_date. Configuration-embedded endpoints are a fallback
            # because they commonly contain hardware type but no lifecycle timestamp.
            endpoints = endpoint_map.get(machine_id, []) or _inline_endpoints(row)
            normalized.append({**machine, **row, **_hierarchy_fields(row),
                               "_id": str(machine_id), "endpoints": endpoints})
        return [_record(row, self._version) for row in normalized]

    def search(self, query: MachineSearchQuery) -> MachinePage:
        limit = min(max(query.limit, 1), 1000)
        criteria = self._filter(query)
        cursor = decode_cursor(query.cursor)
        if cursor:
            criteria["_id"] = {"$gt": self._mongo_id(cursor)}
        raw_rows = list(self.collection.find(criteria, self._projection()).sort("_id", 1).limit(limit + 1).max_time_ms(5000))
        records = self._enrich(raw_rows[:limit])
        # Cohort/eligibility are derived from joined documents, so apply those
        # filters after enrichment while retaining bounded Mongo reads.
        if query.cohort or query.eligible is not None or query.status:
            records = [record for record in records if (not query.cohort or record.resource_cohort == query.cohort) and (query.eligible is None or record.backfill_eligible == query.eligible) and (not query.status or record.status == query.status)]
        next_cursor = encode_cursor(str(raw_rows[limit - 1].get("_id"))) if len(raw_rows) > limit else None
        return MachinePage(records, next_cursor, self._version)

    def get_many(self, machine_ids: Sequence[str]) -> list[MachineRecord]:
        if not machine_ids:
            return []
        ids = [self._mongo_id(value) for value in machine_ids]
        return self._enrich(list(self.collection.find({self.machine_id_field: {"$in": ids}}, self._projection()).max_time_ms(5000)))

    def get_facets(self, query: MachineSearchQuery) -> MachineFacets:
        # Facets are bounded to the normalized projection and are intended for
        # cached/background calls, never per visible table row.
        records: list[MachineRecord] = []
        cursor = self.collection.find(self._filter(query), self._projection()).sort("_id", 1).batch_size(500).max_time_ms(30000)
        for row in cursor:
            records.extend(self._enrich([row]))
        return MachineFacets(_counts(records, "resource_cohort"), _counts(records, "status"), _counts(records, "backfill_eligible"))


def build_mongo_provider() -> tuple[MongoMachineInventoryProvider | None, str]:
    import os
    url = os.getenv("MONGODB_URL") or os.getenv("MONGODB_URI")
    if not url and os.getenv("MONGODB_KEY_VAULT_ENABLED", "1").lower() not in {"0", "false", "no", "off"}:
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
            vault_name = os.getenv("MONGODB_KEY_VAULT_NAME", "metaflow-iac-kv")
            url = SecretClient(vault_url=f"https://{vault_name}.vault.azure.net", credential=DefaultAzureCredential()).get_secret("mongodb-url").value
        except Exception:
            url = None
    username = os.getenv("MONGODB_USERNAME")
    password = os.getenv("MONGODB_PASSWORD")
    endpoint = os.getenv("MONGODB_ENDPOINT") or os.getenv("MONGODB_HOST")
    if not url and not (endpoint and username and password):
        return None, "not_configured"
    try:
        from pymongo import MongoClient
        from pymongo.read_preferences import ReadPreference
        options = dict(read_preference=ReadPreference.SECONDARY_PREFERRED, connectTimeoutMS=3000, serverSelectionTimeoutMS=3000, socketTimeoutMS=5000, retryReads=True)
        if username and password:
            # Passing credentials as arguments avoids URI parsing issues when
            # passwords contain @, &, /, or other reserved characters.
            from urllib.parse import urlparse
            endpoint = endpoint or urlparse(url).hostname
            if not endpoint:
                raise ValueError("MONGODB_ENDPOINT or a valid MONGODB_URL host is required")
            client = MongoClient(endpoint, username=username, password=password, authSource=os.getenv("MONGODB_AUTH_SOURCE", "admin"), **options)
        else:
            client = MongoClient(url, **options)
        client.admin.command("ping")
        database = os.getenv("MONGODB_DATABASE", "production")
        collection = os.getenv("MONGODB_COLLECTION", "machines")
        return MongoMachineInventoryProvider(
            client, database, collection,
            os.getenv("MONGODB_CONFIGURATION_COLLECTION", "machine_configurations"),
            os.getenv("MONGODB_ENDPOINT_COLLECTION", "endpoints"),
        ), "healthy"
    except ModuleNotFoundError:
        return None, "dependency_missing"
    except Exception as exc:
        text = str(exc).lower()
        return None, "unauthorized" if "auth" in text or "not authorized" in text else "unreachable"


def _inline_endpoints(document: dict[str, Any]) -> list[dict[str, Any]]:
    direct = document.get("endpoints")
    if isinstance(direct, list):
        return direct
    result: list[dict[str, Any]] = []
    for component in document.get("components") or []:
        if not isinstance(component, dict):
            continue
        for endpoint in component.get("endpoints") or []:
            if not isinstance(endpoint, dict):
                continue
            nested = endpoint.get("endpoint") if isinstance(endpoint.get("endpoint"), dict) else endpoint
            value = nested.get("type") or nested.get("hardware_type")
            if isinstance(value, str):
                result.append({
                    "type": value,
                    "installation_date": nested.get(
                        "installation_date", nested.get("installationDate")
                    ),
                })
    return result


def _nested_value(document: dict[str, Any], dotted_path: str) -> Any:
    value: Any = document
    for part in dotted_path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _hierarchy_fields(document: dict[str, Any]) -> dict[str, Any]:
    contained = document.get("containedIn") or {}
    company = contained.get("company") or {}
    is_site = str(contained.get("type", "")).lower() in {"facility", "site"}
    return {
        "siteId": str(contained.get("_id")) if is_site and contained.get("_id") is not None else None,
        "siteName": contained.get("name") if is_site else None,
        "organizationId": str(company.get("_id")) if company.get("_id") is not None else None,
        "organizationName": company.get("name"),
    }
