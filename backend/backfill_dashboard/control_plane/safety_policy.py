from __future__ import annotations

import os

from .work_repository import now


def evaluate_campaign(repository, campaign_id: str, *, immediate_reason: str = "") -> bool:
    """Pause new dispatch when repeated or invariant-related failures are observed."""
    threshold = max(1, int(os.getenv("BACKFILL_AUTO_PAUSE_FAILURES", "3")))
    with repository.database.connect() as db:
        campaign = db.execute("SELECT state FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        failures = db.execute(
            "SELECT COUNT(*) FROM work_items WHERE campaign_id=? AND state='failed'", (campaign_id,)
        ).fetchone()[0]
        repeated_submissions = db.execute(
            "SELECT COALESCE(SUM(attempt_count),0) FROM work_items "
            "WHERE campaign_id=? AND state='retry_wait'", (campaign_id,)
        ).fetchone()[0]
        reason = immediate_reason or (
            f"automatic safety pause after {failures} failed work items and "
            f"{repeated_submissions} transient submission failures"
        )
        should_pause = bool(campaign and campaign[0] in {"queued", "running"} and (
            immediate_reason or failures >= threshold or repeated_submissions >= threshold
        ))
        if should_pause:
            db.execute("UPDATE campaigns SET state='paused',paused_at=? WHERE id=?", (now(), campaign_id))
    if should_pause:
        repository.event(campaign_id, "campaign_auto_paused", {"reason": reason})
    return should_pause
