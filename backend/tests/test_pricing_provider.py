from pathlib import Path

from backfill_dashboard.control_plane.database import Database
from backfill_dashboard.control_plane.pricing_provider import AzurePricingProvider


class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *_): return None


def test_azure_pricing_paginates_and_disambiguates_spot(tmp_path, monkeypatch):
    pages = iter([
        {"Items": [{"meterName": "D4s v5", "productName": "Virtual Machines Dsv5", "retailPrice": 1,
                    "currencyCode": "USD", "isPrimaryMeterRegion": True, "unitOfMeasure": "1 Hour"}], "NextPageLink": "next"},
        {"Items": [{"meterName": "D4s v5 Spot", "productName": "Virtual Machines Dsv5", "retailPrice": .2,
                    "currencyCode": "USD", "isPrimaryMeterRegion": True, "unitOfMeasure": "1 Hour"}]},
    ])
    monkeypatch.setattr("backfill_dashboard.control_plane.pricing_provider.urlopen", lambda *_args, **_kwargs: Response(next(pages)))
    monkeypatch.setattr("backfill_dashboard.control_plane.pricing_provider.json.load", lambda response: response.payload)
    provider = AzurePricingProvider(Database(tmp_path / "prices.sqlite3"))
    quote = provider.get_rate("eastus", "Standard_D4s_v5", "spot", refresh=True)
    assert quote["hourly_rate"] == .2
    assert provider.get_rate("eastus", "Standard_D4s_v5", "spot")["source"] == "azure_retail"
