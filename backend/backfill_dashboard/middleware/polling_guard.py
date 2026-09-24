from collections import defaultdict, deque
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AdminPollingGuard:
    """Reject accidental high-frequency workflow polling before it reaches Argo."""

    def __init__(self, *, window_seconds: float = 10, max_requests: int = 4) -> None:
        self.window_seconds = window_seconds
        self.max_requests = max_requests
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, client: str, path: str) -> bool:
        request_times = self._requests[f"{client}:{path}"]
        now = monotonic()
        while request_times and now - request_times[0] >= self.window_seconds:
            request_times.popleft()
        if len(request_times) >= self.max_requests:
            return False
        request_times.append(now)
        return True


def register_polling_guard(app: FastAPI) -> None:
    guard = AdminPollingGuard()
    expensive_paths = {"/api/admin/workflows/summary", "/api/admin/workflows/running"}

    @app.middleware("http")
    async def guard_expensive_workflow_polling(request: Request, call_next):
        if request.method == "GET" and request.url.path in expensive_paths:
            client = request.client.host if request.client else "unknown"
            if not guard.allow(client, request.url.path):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Workflow polling is limited to four requests per 10 seconds."},
                    headers={"Retry-After": "10"},
                )
        return await call_next(request)
