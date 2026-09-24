from __future__ import annotations

import os
import socket

from .campaign_repository import CampaignRepository
from .capacity_service import CapacityService
from .safety_policy import evaluate_campaign


class Dispatcher:
    def __init__(self, repository: CampaignRepository, inventory, capacity: CapacityService, adapter, pricing) -> None:
        self.repository, self.inventory, self.capacity, self.adapter, self.pricing = repository, inventory, capacity, adapter, pricing
        self.owner = f"{socket.gethostname()}:{os.getpid()}"

    def tick(self) -> int:
        self._cancel_requested()
        snapshot, dispatched = self.capacity.snapshot(), 0
        for cohort in ("standard", "ulrpm"):
            profile = snapshot["cohorts"][cohort]
            running = self._active_count(cohort)
            for item in self.repository.lease(cohort, max(0, profile["recommended_concurrency"] - running), self.owner):
                try:
                    current = self.inventory.get_many([item["machine_id"]])
                    if len(current) != 1 or current[0].resource_cohort != cohort or current[0].classification_source_version != item["classifier_version"]:
                        raise ClassificationMismatch("machine classification changed before dispatch")
                    identity = self.adapter.submit(item)
                    price = self.pricing.get_rate(profile["region"], profile["sku"], profile["purchase"]) if profile["region"] and profile["sku"] else None
                    self.repository.submitted(item, identity.workflow_name, identity.run_id, profile, price)
                    dispatched += 1
                except Exception as exc:
                    finder = getattr(self.adapter, "find", lambda _item: None)
                    recovered = None if isinstance(exc, (ClassificationMismatch, ValueError)) else finder(item)
                    if recovered:
                        self.repository.submitted(item, recovered.workflow_name, recovered.run_id, profile, None)
                        dispatched += 1
                    else:
                        self.repository.submit_failed(item, exc, isinstance(exc, (ClassificationMismatch, ValueError)))
                        evaluate_campaign(
                            self.repository, item["campaign_id"],
                            immediate_reason=str(exc) if isinstance(exc, ClassificationMismatch) else "",
                        )
        return dispatched

    def reconcile(self) -> int:
        statuses, changed = self.adapter.statuses(), 0
        with self.repository.database.connect() as db:
            rows = db.execute("SELECT idempotency_key,campaign_id,workflow_name FROM work_items WHERE state IN ('submitted','running')").fetchall()
        for row in rows:
            phase = statuses.get(row["workflow_name"])
            if phase in {"succeeded", "failed", "error"}:
                self.repository.complete(row["idempotency_key"], "succeeded" if phase == "succeeded" else "failed")
                if phase != "succeeded":
                    evaluate_campaign(self.repository, row["campaign_id"])
                changed += 1
            elif phase in {"running", "pending"}:
                with self.repository.database.connect() as db:
                    db.execute("UPDATE work_items SET state=?,started_at=COALESCE(started_at,?) WHERE idempotency_key=?", ("running" if phase == "running" else "submitted", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(), row["idempotency_key"]))
        return changed

    def _active_count(self, cohort: str) -> int:
        with self.repository.database.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM work_items WHERE cohort=? AND state IN ('leased','submitted','running')", (cohort,)).fetchone()[0])

    def _cancel_requested(self) -> None:
        with self.repository.database.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT w.idempotency_key,w.workflow_name FROM work_items w "
                "JOIN campaigns c ON c.id=w.campaign_id LEFT JOIN machine_controls mc "
                "ON mc.campaign_id=w.campaign_id AND mc.machine_id=w.machine_id "
                "WHERE (c.state='cancelling' OR mc.state='cancelled') AND w.state IN ('submitted','running')"
            ).fetchall()
        for row in rows:
            try:
                self.adapter.cancel(row["workflow_name"])
                self.repository.complete(row["idempotency_key"], "cancelled")
            except Exception:
                continue
        with self.repository.database.connect() as db:
            db.execute("UPDATE campaigns SET state='cancelled',completed_at=? WHERE state='cancelling' AND NOT EXISTS (SELECT 1 FROM work_items w WHERE w.campaign_id=campaigns.id AND w.state IN ('submitted','running'))", (__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),))


class ClassificationMismatch(ValueError):
    pass
