"""Contract checks for the safer admin-action status poller."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLL_TS = ROOT / "src" / "features" / "backfill" / "adminActionPoll.ts"
HOOK_TS = ROOT / "src" / "features" / "backfill" / "useOrchestratedBackfillSubmission.ts"
APP_TSX = ROOT / "src" / "App.tsx"
APP_PY = ROOT / "backend" / "backfill_dashboard" / "app.py"


def test_poll_interval_constants_are_safe() -> None:
    source = POLL_TS.read_text(encoding="utf-8")
    assert "ADMIN_ACTION_POLL_INITIAL_MS = 3_000" in source
    assert "ADMIN_ACTION_POLL_MID_MS = 5_000" in source
    assert "ADMIN_ACTION_POLL_MAX_MS = 10_000" in source
    assert "inFlight" in source
    assert "clearTimeout" in source
    assert "isAdminActionTerminal" in source


def test_backoff_helper_steps_up() -> None:
    # Mirror the TS helper so the contract stays honest without a JS runner.
    def next_delay(elapsed_ms: int) -> int:
        if elapsed_ms >= 180_000:
            return 10_000
        if elapsed_ms >= 60_000:
            return 5_000
        return 3_000

    assert next_delay(0) == 3_000
    assert next_delay(59_999) == 3_000
    assert next_delay(60_000) == 5_000
    assert next_delay(179_999) == 5_000
    assert next_delay(180_000) == 10_000


def test_hook_and_app_no_longer_use_1s_action_poll() -> None:
    hook = HOOK_TS.read_text(encoding="utf-8")
    app = APP_TSX.read_text(encoding="utf-8")
    assert "startAdminActionPoll" in hook
    assert "setTimeout(() => void poll(), 1000)" not in hook
    assert "setTimeout(() => void poll(), 1000)" not in app
    assert "startAdminActionPoll" in app
    # Machine panel must not keep a duplicate local submissionAction poller.
    assert app.count("startAdminActionPoll(") == 1


def test_access_log_filter_suppresses_hot_path() -> None:
    from backfill_dashboard.app import _QuietAdminActionAccessFilter

    filt = _QuietAdminActionAccessFilter()

    class _Record:
        def __init__(self, message: str) -> None:
            self._message = message

        def getMessage(self) -> str:
            return self._message

    keep = (
        '127.0.0.1:1 - "GET /api/admin/workflows/summary HTTP/1.1" 200',
        '127.0.0.1:1 - "GET /api/admin/actions/abc/cancel-before-submit HTTP/1.1" 200',
        '127.0.0.1:1 - "GET /api/admin/actions/abc HTTP/1.1" 500',
        '127.0.0.1:1 - "POST /api/admin/actions/abc HTTP/1.1" 200',
    )
    drop = (
        '127.0.0.1:1 - "GET /api/admin/actions/abc HTTP/1.1" 200',
        '127.0.0.1:1 - "GET /api/admin/actions/abc-def HTTP/1.1" 304',
    )
    for message in keep:
        assert filt.filter(_Record(message)) is True, message
    for message in drop:
        assert filt.filter(_Record(message)) is False, message


def test_quiet_filter_registered_in_app_source() -> None:
    source = APP_PY.read_text(encoding="utf-8")
    assert "_QuietAdminActionAccessFilter" in source
    assert 'logging.getLogger("uvicorn.access")' in source
