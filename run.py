"""Start the packaged FastAPI + React dashboard on the host-provided port."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_port(environ: dict[str, str] | None = None) -> int:
    values = os.environ if environ is None else environ
    raw = values.get("DATABRICKS_APP_PORT") or values.get("PORT") or "8000"
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid application port: {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"Application port must be between 1 and 65535, got {port}")
    return port


def main() -> None:
    backend_path = str(Path(__file__).resolve().parent / "backend")
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)
    import uvicorn

    uvicorn.run("backfill_dashboard.app:app", host="0.0.0.0", port=resolve_port())


if __name__ == "__main__":
    main()
