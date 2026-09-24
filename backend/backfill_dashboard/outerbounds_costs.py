"""Read-only Outerbounds cost-report adapter.

The Outerbounds history page is an authenticated UI, not a documented public
API. Deployments provide the JSON endpoint and a read-only bearer token via
environment variables; this keeps credentials server-side and lets the UI
surface a clear configuration state instead of inventing a cost total.
"""
from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen


HISTORY_URL = "https://ui.augury.obp.outerbounds.com/dashboard/costreporting/history"


def current_cost_report() -> dict:
    endpoint = os.getenv("OUTERBOUNDS_COST_REPORT_API_URL", "").strip()
    if not endpoint:
        return {"available": False, "history_url": HISTORY_URL,
                "detail": "Set OUTERBOUNDS_COST_REPORT_API_URL to the authenticated JSON endpoint used by the Outerbounds cost-report page."}
    headers = {"Accept": "application/json"}
    token = os.getenv("OUTERBOUNDS_COST_REPORT_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(endpoint, headers=headers), timeout=12) as response:
            payload = json.load(response)
        return {"available": True, "history_url": HISTORY_URL, "report": payload}
    except Exception as exc:
        return {"available": False, "history_url": HISTORY_URL,
                "detail": f"Outerbounds cost report is unavailable: {str(exc)[:240]}"}
