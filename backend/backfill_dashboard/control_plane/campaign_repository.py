from __future__ import annotations

import json
from typing import Iterable

from ..campaigns import WorkItem
from .database import Database
from .work_repository import WorkRepositoryMixin, now
from .campaign_serialization import campaign_payload

class CampaignRepository(WorkRepositoryMixin):
    def __init__(self, database: Database) -> None:
        self.database = database

    def save_estimate(self, estimate: dict) -> None:
        with self.database.connect() as db:
            db.execute("INSERT INTO estimates VALUES(?,?,?,?,?,?,?,?,?)", (
                estimate["estimate_id"], estimate["estimate_signature"], estimate["created_at"], estimate["expires_at"],
                estimate["inventory_version"], estimate["capacity_digest"], json.dumps(estimate["request"], sort_keys=True),
                json.dumps(estimate, sort_keys=True), int(estimate["production_ready"])))

    def estimate(self, estimate_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute("SELECT result_json FROM estimates WHERE id=?", (estimate_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, campaign: dict) -> None:
        with self.database.connect() as db:
            db.execute("INSERT INTO campaigns(id,name,created_by,created_at,state,inventory_version,selection_snapshot,cohort_counts,date_start,date_end,feature_version,config_digest,estimate_id,estimate_signature,production,submitted_at,paused_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                campaign["id"], campaign["name"], campaign["created_by"], campaign["created_at"], "draft",
                campaign["inventory_version"], json.dumps(campaign["selection_snapshot"], sort_keys=True),
                json.dumps(campaign["cohort_counts"], sort_keys=True), campaign["date_start"], campaign["date_end"],
                campaign["feature_version"], campaign["config_digest"], campaign["estimate_id"], campaign["estimate_signature"],
                int(campaign.get("production", False)), campaign["created_at"], None, None))
        self.event(campaign["id"], "campaign_planning", {"cohort_counts": campaign["cohort_counts"]})

    def finish_planning(self, campaign_id: str, count: int) -> None:
        with self.database.connect() as db:
            db.execute("UPDATE campaigns SET state='queued' WHERE id=? AND state='draft'", (campaign_id,))
        self.event(campaign_id, "campaign_queued", {"work_item_count": count})

    def planning_failed(self, campaign_id: str, error: Exception) -> None:
        with self.database.connect() as db:
            db.execute("UPDATE campaigns SET state='failed',completed_at=? WHERE id=?", (now(), campaign_id))
        self.event(campaign_id, "campaign_planning_failed", {"error": str(error)[:500]})

    def add_work(self, items: Iterable[WorkItem], profile_versions: dict[str, str]) -> int:
        rows = [(item.idempotency_key, item.campaign_id, item.machine_id, item.cohort, item.window_start.isoformat(),
                 item.window_end.isoformat(), item.sequence_number, item.state, item.classifier_version,
                 profile_versions[item.cohort]) for item in items]
        if not rows:
            return 0
        with self.database.connect() as db:
            db.execute("BEGIN")
            db.executemany("INSERT OR IGNORE INTO work_items(idempotency_key,campaign_id,machine_id,cohort,window_start,window_end,sequence_number,state,classifier_version,resource_profile_version) VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
            db.execute("COMMIT")
        return len(rows)

    def get(self, campaign_id: str) -> dict | None:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            counts = db.execute("SELECT state,COUNT(*) total FROM work_items WHERE campaign_id=? GROUP BY state", (campaign_id,)).fetchall()
        return campaign_payload(row, counts) if row else None

    def list(self, limit: int = 50) -> list[dict]:
        with self.database.connect() as db:
            rows = db.execute("SELECT id FROM campaigns ORDER BY created_at DESC LIMIT ?", (min(limit, 200),)).fetchall()
        return [value for row in rows if (value := self.get(row[0]))]

    def items(self, campaign_id: str, cursor: int = 0, limit: int = 100) -> dict:
        with self.database.connect() as db:
            rows = db.execute("SELECT rowid,* FROM work_items WHERE campaign_id=? AND rowid>? ORDER BY rowid LIMIT ?", (campaign_id, cursor, min(limit, 200) + 1)).fetchall()
        page = rows[:min(limit, 200)]
        return {"items": [dict(row) for row in page], "next_cursor": page[-1]["rowid"] if len(rows) > len(page) else None}

    def set_state(self, campaign_id: str, action: str, actor: str = "unknown", reason: str = "") -> dict:
        transitions = {"pause": (("queued", "running"), "paused"), "resume": (("paused",), "queued"), "cancel": (("queued", "running", "paused"), "cancelling")}
        allowed, target = transitions[action]
        with self.database.connect() as db:
            row = db.execute("SELECT state FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not row or row[0] not in allowed:
                raise ValueError(f"cannot {action} campaign from {row[0] if row else 'missing'}")
            db.execute("UPDATE campaigns SET state=?,paused_at=? WHERE id=?", (target, now() if target == "paused" else None, campaign_id))
            if action == "cancel":
                db.execute("UPDATE work_items SET state='cancelled',finished_at=? WHERE campaign_id=? AND state IN ('blocked','ready','retry_wait','leased')", (now(), campaign_id))
        self.event(campaign_id, f"campaign_{action}", {"actor": actor, "reason": reason})
        return self.get(campaign_id) or {}

    def event(self, campaign_id: str, kind: str, payload: dict) -> None:
        with self.database.connect() as db:
            db.execute("INSERT INTO campaign_events(campaign_id,kind,payload,created_at) VALUES(?,?,?,?)", (campaign_id, kind, json.dumps(payload), now()))

    def events(self, campaign_id: str, after: int = 0) -> list[dict]:
        with self.database.connect() as db:
            rows = db.execute("SELECT * FROM campaign_events WHERE campaign_id=? AND id>? ORDER BY id LIMIT 200", (campaign_id, after)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
