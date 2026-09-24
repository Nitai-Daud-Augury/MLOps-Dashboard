from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def allow(self, key: str, interval_seconds: float) -> bool:
        current = time.monotonic()
        with self._lock:
            previous = self._last.get(key, 0.0)
            if current - previous < interval_seconds:
                return False
            self._last[key] = current
        return True


limiter = RateLimiter()
