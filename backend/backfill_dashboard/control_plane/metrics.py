from __future__ import annotations

from .database import Database


def control_plane_metrics(database: Database) -> dict:
    with database.connect() as db:
        queues = {row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM work_items GROUP BY state")}
        cohorts = {row[0]: row[1] for row in db.execute("SELECT cohort,COUNT(*) FROM work_items GROUP BY cohort")}
        campaigns = {row[0]: row[1] for row in db.execute("SELECT state,COUNT(*) FROM campaigns GROUP BY state")}
        oldest = db.execute("SELECT MIN(c.created_at) FROM work_items w JOIN campaigns c ON c.id=w.campaign_id WHERE w.state IN ('ready','retry_wait')").fetchone()[0]
    return {"campaigns": campaigns, "work_items_by_state": queues, "work_items_by_cohort": cohorts,
            "oldest_ready_campaign_at": oldest}
