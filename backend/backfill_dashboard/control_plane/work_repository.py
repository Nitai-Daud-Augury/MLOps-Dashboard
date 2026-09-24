from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkRepositoryMixin:
    def lease(self, cohort: str, limit: int, owner: str, lease_seconds: int = 120) -> list[dict]:
        if limit <= 0:
            return []
        current, expiry = now(), (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE work_items SET state='ready',lease_owner=NULL,lease_expires_at=NULL WHERE state='leased' AND lease_expires_at<? AND workflow_name IS NULL", (current,))
            rows = db.execute(
                "SELECT w.* FROM work_items w JOIN campaigns c ON c.id=w.campaign_id WHERE w.cohort=? AND w.state IN ('ready','retry_wait') AND (w.next_attempt_at IS NULL OR w.next_attempt_at<=?) AND c.state IN ('queued','running') AND NOT EXISTS (SELECT 1 FROM machine_controls mc WHERE mc.campaign_id=w.campaign_id AND mc.machine_id=w.machine_id AND mc.state IN ('paused','cancelled')) AND NOT EXISTS (SELECT 1 FROM work_items a WHERE a.machine_id=w.machine_id AND a.state IN ('leased','submitted','running')) ORDER BY c.created_at,w.sequence_number LIMIT ?", (cohort, current, limit)).fetchall()
            for row in rows:
                db.execute("UPDATE work_items SET state='leased',lease_owner=?,lease_expires_at=? WHERE idempotency_key=?", (owner, expiry, row["idempotency_key"]))
            ids = {row["campaign_id"] for row in rows}
            db.executemany("UPDATE campaigns SET state='running' WHERE id=? AND state='queued'", ((value,) for value in ids))
            db.execute("COMMIT")
        return [{**dict(row), "state": "leased", "lease_owner": owner, "lease_expires_at": expiry} for row in rows]

    def submitted(self, item: dict, workflow_name: str, run_id: str | None, resource: dict, price: dict | None) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = int(item["attempt_count"]) + 1
            db.execute("UPDATE work_items SET state='submitted',attempt_count=?,workflow_name=?,run_id=?,lease_owner=NULL,lease_expires_at=NULL WHERE idempotency_key=?", (attempt, workflow_name, run_id, item["idempotency_key"]))
            db.execute("INSERT OR IGNORE INTO attempts(work_item_id,attempt_number,workflow_name,run_id,resource_request,price_snapshot,queued_at) VALUES(?,?,?,?,?,?,?)", (item["idempotency_key"], attempt, workflow_name, run_id, json.dumps(resource), json.dumps(price) if price else None, now()))
            db.execute("COMMIT")
        self.event(item["campaign_id"], "work_submitted", {"work_item": item["idempotency_key"], "machine_id": item["machine_id"]})

    def submit_failed(self, item: dict, error: Exception, permanent: bool = False) -> None:
        attempt = int(item["attempt_count"]) + 1
        state = "failed" if permanent or attempt >= 5 else "retry_wait"
        delay = min(900, 2 ** attempt * 5)
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay + random.uniform(0, delay * .25))).isoformat() if state == "retry_wait" else None
        with self.database.connect() as db:
            db.execute("UPDATE work_items SET state=?,attempt_count=?,next_attempt_at=?,lease_owner=NULL,lease_expires_at=NULL,error_code=?,error_summary=? WHERE idempotency_key=?", (state, attempt, next_at, type(error).__name__, str(error)[:500], item["idempotency_key"]))
        self.event(item["campaign_id"], "work_submit_failed", {"work_item": item["idempotency_key"], "state": state})

    def complete(self, key: str, outcome: str, metrics: dict | None = None) -> None:
        terminal = "succeeded" if outcome == "succeeded" else "failed" if outcome == "failed" else "cancelled"
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT * FROM work_items WHERE idempotency_key=?", (key,)).fetchone()
            if not item:
                db.execute("ROLLBACK")
                return
            db.execute("UPDATE work_items SET state=?,finished_at=?,runtime_metrics=? WHERE idempotency_key=?", (terminal, now(), json.dumps(metrics or {}), key))
            db.execute("UPDATE attempts SET outcome=?,finished_at=? WHERE work_item_id=? AND attempt_number=?", (terminal, now(), key, item["attempt_count"]))
            if terminal == "succeeded":
                db.execute("UPDATE work_items SET state='ready' WHERE campaign_id=? AND machine_id=? AND sequence_number=? AND state='blocked'", (item["campaign_id"], item["machine_id"], item["sequence_number"] + 1))
            remaining = db.execute("SELECT COUNT(*) FROM work_items WHERE campaign_id=? AND state NOT IN ('succeeded','failed','cancelled')", (item["campaign_id"],)).fetchone()[0]
            if remaining == 0:
                failures = db.execute("SELECT COUNT(*) FROM work_items WHERE campaign_id=? AND state='failed'", (item["campaign_id"],)).fetchone()[0]
                db.execute("UPDATE campaigns SET state=?,completed_at=? WHERE id=?", ("failed" if failures else "completed", now(), item["campaign_id"]))
            db.execute("COMMIT")
        self.event(item["campaign_id"], "work_completed", {"work_item": key, "outcome": terminal})

    def resolve_item(self, campaign_id: str, key: str, action: str, actor: str, reason: str) -> dict:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT * FROM work_items WHERE campaign_id=? AND idempotency_key=?", (campaign_id, key)).fetchone()
            if not item or item["state"] != "failed":
                db.execute("ROLLBACK")
                raise ValueError("only a failed work item can be retried or skipped")
            target = "ready" if action == "retry" else "cancelled"
            db.execute("UPDATE work_items SET state=?,next_attempt_at=NULL,error_code=NULL,error_summary=NULL WHERE idempotency_key=?", (target, key))
            if action == "skip":
                db.execute("UPDATE work_items SET state='ready' WHERE campaign_id=? AND machine_id=? AND sequence_number=? AND state='blocked'", (campaign_id, item["machine_id"], item["sequence_number"] + 1))
            db.execute("UPDATE campaigns SET state='queued',completed_at=NULL WHERE id=? AND state='failed'", (campaign_id,))
            db.execute("COMMIT")
        self.event(campaign_id, f"work_{action}", {"work_item": key, "actor": actor, "reason": reason})
        return self.get(campaign_id) or {}
