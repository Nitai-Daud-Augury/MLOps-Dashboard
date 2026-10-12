"""Durable copy of the latest scan snapshot, so results survive an app restart.

After every successful scan the snapshot is saved in a versioned envelope:

    {"format": "mlops-dashboard-scan-snapshot", "version": 1, "saved_at": ..., "snapshot": {...}}

* ``azure_blob`` (default on Databricks Apps): gzip JSON at
  ``<FST_BACKFILL_ACCOUNT_NAME>/<FST_BACKFILL_CONTAINER_NAME>/dashboard-control/_scan/latest_scan_snapshot.v1.json.gz``
  (default aifleetmlopsfstbackfill/fst-backfill) with the same credential as the control
  store / manifests (FST_BACKFILL_SAS_TOKEN or the account key). The container is private;
  machine ids never go to the public GitHub artifacts.
* ``local`` (default elsewhere, and the fallback when the blob write fails): the same
  envelope in a local file next to BACKFILL_DASHBOARD_STATE_PATH.
* ``off``: disabled.

A load whose format/version does not match, or whose snapshot is malformed, is ignored
and logged; the dashboard then simply shows "no scan yet" until the next scan.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .runtime_mode import resolve_runtime

FORMAT = "mlops-dashboard-scan-snapshot"
VERSION = 1
STORE_ENV = "SCAN_SNAPSHOT_STORE"
BLOB_NAME = f"dashboard-control/_scan/latest_scan_snapshot.v{VERSION}.json.gz"
_KINDS = {"azure_blob", "local", "off"}


def _log(message: str) -> None:
    print(f"[scan-snapshot] {message}", flush=True)


def envelope(snapshot: dict, *, now: datetime | None = None) -> dict:
    return {"format": FORMAT, "version": VERSION,
            "saved_at": (now or datetime.now(timezone.utc)).isoformat(), "snapshot": snapshot}


def unwrap(payload: object, *, origin: str, log: Callable[[str], None] = _log) -> tuple[dict, str] | None:
    """(snapshot, saved_at) for a valid envelope of this version, else None (logged)."""
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        log(f"ignored {origin}: not a scan snapshot envelope")
        return None
    if payload.get("version") != VERSION:
        log(f"ignored {origin}: version {payload.get('version')!r} != {VERSION} (rescan to refresh)")
        return None
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("machines"), list) or not isinstance(snapshot.get("summary"), dict):
        log(f"ignored {origin}: malformed snapshot")
        return None
    return snapshot, str(payload.get("saved_at") or "")


class ScanSnapshotStore:
    def __init__(self, *, kind: str, local_path: Path, account_name: str = "", container_name: str = "",
                 blob_factory: Callable[[], object] | None = None, log: Callable[[str], None] = _log) -> None:
        if kind not in _KINDS:
            raise RuntimeError(f"{STORE_ENV} must be one of {sorted(_KINDS)}")
        self.kind = kind
        self.local_path = local_path
        self.account_name = account_name
        self.container_name = container_name
        self._blob_factory = blob_factory or self._default_blob
        self._log = log

    @classmethod
    def from_env(cls, state_path: Path, environ: Mapping[str, str] | None = None) -> "ScanSnapshotStore":
        values = environ if environ is not None else os.environ
        # Durable blob only where the control store is the blob (the Apps default); local dev
        # and tests keep a local file and never touch Azure.
        on_apps_blob = (resolve_runtime(values) == "databricks"
                        and values.get("BACKFILL_CONTROL_STORE", "azure_blob").lower() == "azure_blob")
        default = "azure_blob" if on_apps_blob else "local"
        kind = (values.get(STORE_ENV) or default).strip().lower()
        return cls(kind=kind, local_path=state_path.with_name(f"ulrpm_scan_snapshot.v{VERSION}.json.gz"),
                   account_name=values.get("FST_BACKFILL_ACCOUNT_NAME", "aifleetmlopsfstbackfill"),
                   container_name=values.get("FST_BACKFILL_CONTAINER_NAME", "fst-backfill"))

    def describe(self) -> str:
        if self.kind == "azure_blob":
            return f"azure_blob:{self.account_name}/{self.container_name}/{BLOB_NAME}"
        return "off" if self.kind == "off" else f"local:{self.local_path}"

    def _default_blob(self):
        from azure.storage.blob import BlobClient
        from .credentials import ManifestCredentialResolver
        return BlobClient(account_url=f"https://{self.account_name}.blob.core.windows.net",
                          container_name=self.container_name, blob_name=BLOB_NAME,
                          credential=ManifestCredentialResolver().resolve().value)

    @staticmethod
    def _encode(payload: dict) -> bytes:
        return gzip.compress(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def _save_local(self, data: bytes) -> str:
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.local_path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(self.local_path)
        return f"local:{self.local_path}"

    def save(self, snapshot: dict) -> str | None:
        """Persist; returns where it landed, or None. Never raises (a scan must not fail on it)."""
        if self.kind == "off":
            return None
        data = self._encode(envelope(snapshot))
        if self.kind == "azure_blob":
            try:
                self._blob_factory().upload_blob(data, overwrite=True)
                where = self.describe()
                self._log(f"saved {len(snapshot.get('machines') or [])} machines to {where}")
                return where
            except Exception as exc:  # noqa: BLE001
                self._log(f"blob save failed ({type(exc).__name__}); falling back to a local file (lost on restart)")
        try:
            where = self._save_local(data)
            self._log(f"saved {len(snapshot.get('machines') or [])} machines to {where}")
            return where
        except Exception as exc:  # noqa: BLE001
            self._log(f"local save failed: {type(exc).__name__}: {exc}")
            return None

    def _read(self) -> tuple[bytes, str] | None:
        if self.kind == "azure_blob":
            try:
                return self._blob_factory().download_blob().readall(), self.describe()
            except Exception as exc:  # noqa: BLE001
                missing = getattr(exc, "status_code", None) == 404 or getattr(exc, "error_code", None) == "BlobNotFound"
                self._log("no saved snapshot in blob yet" if missing else f"blob load failed ({type(exc).__name__}); trying the local file")
        if self.local_path.exists():
            return self.local_path.read_bytes(), f"local:{self.local_path}"
        return None

    def load(self) -> dict | None:
        """{"snapshot", "saved_at", "loaded_from"} or None. Never raises."""
        if self.kind == "off":
            return None
        try:
            raw = self._read()
            if raw is None:
                return None
            data, origin = raw
            payload = json.loads(gzip.decompress(data).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._log(f"ignored saved snapshot: unreadable ({type(exc).__name__})")
            return None
        result = unwrap(payload, origin=origin, log=self._log)
        if result is None:
            return None
        snapshot, saved_at = result
        self._log(f"loaded {len(snapshot['machines'])} machines from {origin} (saved {saved_at})")
        return {"snapshot": snapshot, "saved_at": saved_at, "loaded_from": origin}
