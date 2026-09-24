import json


def campaign_payload(row, counts) -> dict:
    result = dict(row)
    for field in ("selection_snapshot", "cohort_counts"):
        result[field] = json.loads(result[field])
    result["work_item_counts"] = {item["state"]: item["total"] for item in counts}
    result["production"] = bool(result.get("production"))
    return result
