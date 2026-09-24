from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path


class EstimateSigner:
    def __init__(self, database_path: Path) -> None:
        secret = os.getenv("BACKFILL_ESTIMATE_SIGNING_KEY")
        self.production_ready = bool(secret)
        self.secret = (secret or f"local-only:{database_path.resolve()}").encode()

    def sign(self, payload: dict) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.secret, body, hashlib.sha256).hexdigest()

    def valid(self, payload: dict, signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)
