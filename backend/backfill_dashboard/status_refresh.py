"""Rate-limited, on-demand workflow status refresh.

On the GitHub Actions backend each status refresh is a full GitHub Actions run, so the
dashboard never refreshes in the background. A refresh starts only on

* an explicit Refresh click (``reason="manual"``), limited per user and globally, or
* a dispatch completion event (``reason="event"``): a trigger/terminate whose GitHub run
  finished with a workflow id. Events are not limited per user; if the global cooldown is
  active, ONE deferred refresh is scheduled for when it ends.

At most one refresh is in flight (the provider is single-flight); concurrent requests join
it. A limited request gets the cached snapshot plus ``retry_after`` seconds. Spamming the
endpoint therefore never creates more than one GitHub run per cooldown.
"""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Callable, Mapping

COOLDOWN_ENV = "WORKFLOW_STATUS_REFRESH_COOLDOWN_SECONDS"
USER_COOLDOWN_ENV = "WORKFLOW_STATUS_REFRESH_USER_COOLDOWN_SECONDS"
DEFAULT_COOLDOWN_SECONDS = 60


def _seconds(name: str, environ: Mapping[str, str] | None) -> int:
    raw = (environ if environ is not None else os.environ).get(name, "")
    try:
        return max(10, min(int(raw), 3600)) if raw else DEFAULT_COOLDOWN_SECONDS
    except ValueError:
        return DEFAULT_COOLDOWN_SECONDS


def _default_timer(delay: float, callback: Callable[[], None]) -> None:
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()


class StatusRefreshGate:
    def __init__(self, provider, *, environ: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 timer: Callable[[float, Callable[[], None]], None] = _default_timer,
                 log: Callable[[str], None] | None = None) -> None:
        self._provider = provider
        self._environ = environ
        self._clock = clock
        self._timer = timer
        self._log = log or (lambda message: print(f"[workflow-status] {message}", flush=True))
        self._lock = threading.Lock()
        self._last_start: float | None = None
        self._last_user: dict[str, float] = {}
        self._deferred = False

    @property
    def cooldown(self) -> int:
        return _seconds(COOLDOWN_ENV, self._environ)

    @property
    def user_cooldown(self) -> int:
        return _seconds(USER_COOLDOWN_ENV, self._environ)

    def _global_wait(self, now: float) -> float:
        # Measured from the later of the last start and the last completion, so a slow
        # refresh is followed by a full cooldown before the next one.
        marks = [mark for mark in (self._last_start, self._provider.last_completed()) if mark is not None]
        return max(0.0, max(marks) + self.cooldown - now) if marks else 0.0

    def request(self, user: str | None = None, reason: str = "manual") -> dict:
        user = (user or "anonymous").strip().lower() or "anonymous"
        with self._lock:
            if self._provider.refreshing:
                return {"state": "joined", "retry_after": None, "reason": reason}
            now = self._clock()
            wait = self._global_wait(now)
            if reason == "manual":
                last_user = self._last_user.get(user)
                if last_user is not None:
                    wait = max(wait, last_user + self.user_cooldown - now)
                if wait > 0:
                    return {"state": "limited", "retry_after": math.ceil(wait), "reason": reason}
            elif wait > 0:
                if not self._deferred:
                    self._deferred = True
                    self._timer(wait, self._fire_deferred)
                    self._log(f"event refresh deferred {math.ceil(wait)}s (global cooldown)")
                return {"state": "deferred", "retry_after": math.ceil(wait), "reason": reason}
            if not self._provider.start_refresh():
                return {"state": "joined", "retry_after": None, "reason": reason}
            self._last_start = now
            if reason == "manual":
                self._last_user[user] = now
            self._log(f"refresh started (reason={reason})")
            return {"state": "started", "retry_after": None, "reason": reason}

    def _fire_deferred(self) -> None:
        with self._lock:
            self._deferred = False
        self.request("dispatch-event", reason="event")

    def status(self, user: str | None = None) -> dict:
        """Non-mutating: when the given user could refresh next (for the UI's 'retry in Ns')."""
        user = (user or "anonymous").strip().lower() or "anonymous"
        with self._lock:
            now = self._clock()
            wait = self._global_wait(now)
            last_user = self._last_user.get(user)
            if last_user is not None:
                wait = max(wait, last_user + self.user_cooldown - now)
            return {"refreshing": bool(self._provider.refreshing),
                    "retry_after": math.ceil(wait) if wait > 0 else None,
                    "cooldown_seconds": self.cooldown, "user_cooldown_seconds": self.user_cooldown}


def is_dispatch_completion(action: Mapping) -> bool:
    """A finished trigger/terminate whose result names a workflow (the run to refresh status for)."""
    if action.get("action") not in {"trigger", "terminate"}:
        return False
    if action.get("action") == "trigger":
        return bool(action.get("workflow_id"))
    return action.get("status") == "succeeded"
