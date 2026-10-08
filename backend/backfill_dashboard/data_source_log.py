"""One greppable logger for data-source decisions.

Every line starts with ``[data-source]`` (``grep '\\[data-source\\]'``). The
logger is ``uvicorn.error.data_source``: a child of ``uvicorn.error``, so with
uvicorn's default logging config (``npm run api``) INFO lines reach the API
terminal through uvicorn's own stderr handler, formatted like other uvicorn
lines (``INFO:     [data-source] ...``). Outside uvicorn (scripts, tests) the
records propagate to the root logger as usual.

Messages never include credentials: error text is passed through ``redact``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

LOGGER_NAME = "uvicorn.error.data_source"
PREFIX = "[data-source]"
logger = logging.getLogger(LOGGER_NAME)

_REDACTIONS = (
    (re.compile(r"mongodb(?:\+srv)?://\S+", re.IGNORECASE), "mongodb://<redacted>"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|access_token|sig|accountkey|account_key|key)=([^\s&;,'\"]+)"), r"\1=<redacted>"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer <redacted>"),
    (re.compile(r"\bdapi[0-9a-f]{16,}\b", re.IGNORECASE), "<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"), "<redacted-jwt>"),
)


def redact(text: Any) -> str:
    value = str(text)
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def error_text(exc: BaseException, limit: int = 300) -> str:
    return f"{type(exc).__name__}: {redact(exc)[:limit]}"


def info(message: str) -> None:
    logger.info("%s %s", PREFIX, message)


def warning(message: str) -> None:
    logger.warning("%s %s", PREFIX, message)


def describe_lifecycle_provider(provider: Any) -> str:
    """Human description of the lifecycle provider object actually in use."""
    if provider is None:
        return "none"
    warehouse = getattr(provider, "warehouse_id", None)
    table = getattr(provider, "table", None)
    if warehouse and table:
        auth = getattr(provider, "auth_description", None)
        if not isinstance(auth, str):
            auth = f"profile={getattr(provider, 'profile', None)}"
        return f"databricks bronze machines_raw table={table} warehouse={warehouse} {auth}"
    machines = getattr(provider, "machines", None)
    if machines is not None and hasattr(provider, "endpoints"):
        names = []
        for attr in ("collection", "machines", "endpoints"):
            name = getattr(getattr(provider, attr, None), "full_name", None)
            if name:
                names.append(str(name))
        return "mongo collections=" + (",".join(names) or "unknown")
    return type(provider).__name__
