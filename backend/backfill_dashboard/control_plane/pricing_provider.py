from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from urllib.request import urlopen

from .database import Database

AZURE_PRICES_URL = "https://prices.azure.com/api/retail/prices"


class AzurePricingProvider:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_rate(self, region: str, sku: str, purchase: str, refresh: bool = False) -> dict:
        key = f"{region}|{sku}|{purchase}"
        override = os.getenv(f"BACKFILL_PRICE_{purchase.upper()}_{sku.upper().replace('-', '_')}")
        if override:
            return _quote(region, sku, purchase, float(override), "configured_override", "")
        cached = self._cached(key)
        if cached and (not refresh or _fresh(cached["retrieved_at"], 24)):
            return cached
        if not refresh:
            return cached or _missing(region, sku, purchase)
        try:
            result = self._fetch(region, sku, purchase)
            with self.database.connect() as db:
                db.execute("INSERT OR REPLACE INTO price_quotes VALUES(?,?,?)", (key, json.dumps(result), result["retrieved_at"]))
            return result
        except Exception as exc:
            return cached or {**_missing(region, sku, purchase), "error": str(exc)[:300]}

    def _cached(self, key: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute("SELECT payload FROM price_quotes WHERE cache_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _fetch(self, region: str, sku: str, purchase: str) -> dict:
        query = f"armRegionName eq '{region}' and armSkuName eq '{sku}' and serviceName eq 'Virtual Machines' and priceType eq 'Consumption'"
        url = f"{AZURE_PRICES_URL}?$filter={quote(query)}"
        matches = []
        while url:
            with urlopen(url, timeout=8) as response:
                payload = json.load(response)
            matches.extend(item for item in payload.get("Items", []) if _purchase_match(item, purchase))
            url = payload.get("NextPageLink")
        linux = [item for item in matches if "windows" not in str(item.get("productName", "")).lower()
                 and item.get("isPrimaryMeterRegion", True) and item.get("unitOfMeasure", "1 Hour") == "1 Hour"]
        if len(linux) != 1:
            raise ValueError(f"Azure price lookup returned {len(linux)} unambiguous Linux meters")
        item = linux[0]
        return _quote(region, sku, purchase, float(item["retailPrice"]), "azure_retail", AZURE_PRICES_URL, item.get("currencyCode", "USD"))


def _purchase_match(item: dict, purchase: str) -> bool:
    text = " ".join(str(item.get(key, "")) for key in ("meterName", "productName", "skuName")).lower()
    is_spot = "spot" in text or "low priority" in text
    return is_spot if purchase == "spot" else not is_spot


def _quote(region: str, sku: str, purchase: str, rate: float, source: str, url: str, currency: str = "USD") -> dict:
    return {"region": region, "sku": sku, "purchase": purchase, "hourly_rate": rate, "currency": currency,
            "source": source, "source_url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(), "available": True}


def _missing(region: str, sku: str, purchase: str) -> dict:
    return {"region": region, "sku": sku, "purchase": purchase, "hourly_rate": None, "currency": "USD",
            "source": "unavailable", "source_url": AZURE_PRICES_URL, "retrieved_at": None, "available": False}


def _fresh(timestamp: str, hours: int) -> bool:
    return datetime.fromisoformat(timestamp) >= datetime.now(timezone.utc) - timedelta(hours=hours)
