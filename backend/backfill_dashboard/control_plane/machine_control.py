from __future__ import annotations

from .work_repository import now


def apply_machine_action(repository, campaign_id: str, machine_id: str, action: str,
                         actor: str, reason: str) -> dict:
    with repository.database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        exists = db.execute(
            "SELECT 1 FROM work_items WHERE campaign_id=? AND machine_id=? LIMIT 1",
            (campaign_id, machine_id),
        ).fetchone()
        if not exists:
            db.execute("ROLLBACK")
            raise ValueError("machine is not part of this campaign")
        if action == "resume":
            current = db.execute(
                "SELECT state FROM machine_controls WHERE campaign_id=? AND machine_id=?",
                (campaign_id, machine_id),
            ).fetchone()
            if not current or current[0] != "paused":
                db.execute("ROLLBACK")
                raise ValueError("only a paused machine can be resumed")
            db.execute("DELETE FROM machine_controls WHERE campaign_id=? AND machine_id=?", (campaign_id, machine_id))
        else:
            target = {"pause": "paused", "cancel": "cancelled"}[action]
            db.execute(
                "INSERT OR REPLACE INTO machine_controls VALUES(?,?,?,?,?,?)",
                (campaign_id, machine_id, target, reason, actor, now()),
            )
            if action == "cancel":
                db.execute(
                    "UPDATE work_items SET state='cancelled',finished_at=? WHERE campaign_id=? AND machine_id=? "
                    "AND state IN ('blocked','ready','retry_wait','leased')", (now(), campaign_id, machine_id),
                )
        db.execute("COMMIT")
    repository.event(campaign_id, f"machine_{action}", {
        "machine_id": machine_id, "actor": actor, "reason": reason,
    })
    return {"campaign_id": campaign_id, "machine_id": machine_id,
            "state": "active" if action == "resume" else target}
