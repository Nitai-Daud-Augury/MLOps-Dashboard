"""Which store the scan reads Feature Store coverage from: ``blob`` or ``databricks``.

* ``FEATURES_SOURCE=blob|databricks`` is the default (``blob`` when unset or invalid).
* The UI can switch it at runtime (``PUT /api/settings/features-source``). The
  choice is persisted as the ``features_source`` setting in the backfill control
  store (``BACKFILL_CONTROL_STORE``: Azure blob ``dashboard-control/_settings/``
  by default, the UC control table, or local JSON). If the control store cannot
  be written (e.g. no manifest/control credential), the value is kept in memory
  and in a local JSON file next to the scan state, and the API says it is not
  durable (on Databricks Apps that file is under /tmp and is lost on restart).
* Loading the persisted value never blocks a request: the first read starts a
  background load and the env default applies until it finishes.

The switch never touches running backfills or admin actions; it only selects
the reader for the *next* scan. Results are tagged with the source they were
computed from (``snapshot.features_source``), so a mismatch is visible until a
new scan completes.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import data_source_log as ds_log

FEATURES_SOURCES = ("blob", "databricks")
DEFAULT_FEATURES_SOURCE = "blob"
SETTING_NAME = "features_source"
SOURCE_LABELS = {"blob": "Azure Blob (FST Parquet)", "databricks": "Databricks feature_store"}


class FeaturesSourceConflict(RuntimeError):
    """Switching is refused while a scan is running (HTTP 409)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_source(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in FEATURES_SOURCES else None


def env_default(environ: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    """(default source, config error) from ``FEATURES_SOURCE``."""
    raw = (os.environ if environ is None else environ).get("FEATURES_SOURCE")
    if raw is None or not str(raw).strip():
        return DEFAULT_FEATURES_SOURCE, None
    source = normalize_source(raw)
    if source is None:
        return DEFAULT_FEATURES_SOURCE, f"invalid FEATURES_SOURCE={str(raw)[:40]!r}; expected blob or databricks (using blob)"
    return source, None


class FeaturesSourceSetting:
    def __init__(self, store: Any = None, *, fallback_path: Path | None = None,
                 environ: Mapping[str, str] | None = None, load_in_background: bool = True) -> None:
        self.store = store
        self.fallback_path = fallback_path
        self.default, self.config_error = env_default(environ)
        self._lock = threading.Lock()
        self._record: dict[str, Any] | None = None  # persisted/switched value
        self._origin = "env_default"
        self._load_state = "not_loaded"  # not_loaded | loading | loaded | not_found | error
        self._load_error: str | None = None
        self._durable: bool | None = None
        self._persist_error: str | None = None
        self._background = load_in_background

    # -- persistence ---------------------------------------------------------------
    def _location(self) -> str | None:
        try:
            return self.store.describe_settings_location(SETTING_NAME) if self.store is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _read_fallback(self) -> dict | None:
        if self.fallback_path is None:
            return None
        try:
            return json.loads(self.fallback_path.read_text(encoding="utf-8")).get(SETTING_NAME)
        except (FileNotFoundError, ValueError, AttributeError):
            return None

    def _write_fallback(self, record: dict) -> None:
        if self.fallback_path is None:
            return
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        self.fallback_path.write_text(json.dumps({SETTING_NAME: record}, indent=2, sort_keys=True), encoding="utf-8")

    def load(self) -> None:
        """Read the persisted value (blocking). Safe to call more than once."""
        record, state, error, durable = None, "not_found", None, None
        if self.store is not None:
            try:
                record = self.store.get_setting(SETTING_NAME)
                state, durable = ("loaded" if record else "not_found"), True
            except Exception as exc:  # noqa: BLE001 - fail-open to the local fallback / env default
                state, error = "error", ds_log.error_text(exc, 500)
        if record is None and state != "loaded":
            local = self._read_fallback()
            if local:
                record, durable = local, False
                state = "loaded" if state != "error" else "error"
        value = normalize_source((record or {}).get("value"))
        with self._lock:
            if self._origin == "switched":
                return  # a switch in this process wins over a slower load
            self._load_state, self._load_error = state, error
            if value:
                self._record, self._origin, self._durable = dict(record or {}, value=value), "persisted", durable
        if error:
            ds_log.warning(f"features source: could not read persisted setting from {self._location()}: {error} -> using "
                           f"{value or self.default} ({'local fallback' if value else 'FEATURES_SOURCE default'})")
        elif value:
            ds_log.info(f"features source: loaded persisted setting {value} from {self._location() if durable else self.fallback_path}")

    def _ensure_loaded(self) -> None:
        with self._lock:
            if self._load_state != "not_loaded":
                return
            self._load_state = "loading"
        if self._background:
            threading.Thread(target=self.load, name="features-source-load", daemon=True).start()
        else:
            self.load()

    # -- API -------------------------------------------------------------------------
    def current(self) -> str:
        self._ensure_loaded()
        with self._lock:
            return (self._record or {}).get("value") or self.default

    def describe(self, *, load: bool = True) -> dict[str, Any]:
        if load:
            self._ensure_loaded()
        with self._lock:
            record = dict(self._record or {})
            source = record.get("value") or self.default
            return {
                "source": source,
                "label": SOURCE_LABELS[source],
                "options": list(FEATURES_SOURCES),
                "default": self.default,
                "origin": self._origin,  # env_default | persisted | switched
                "updated_at": record.get("updated_at"),
                "updated_by": record.get("updated_by"),
                "previous": record.get("previous"),
                "persistence": {
                    "location": self._location(),
                    "load_state": self._load_state,
                    "load_error": self._load_error,
                    "durable": self._durable,
                    "error": self._persist_error,
                    "fallback_path": str(self.fallback_path) if self.fallback_path else None,
                },
                "config_error": self.config_error,
            }

    def switch(self, source: Any, *, actor: str | None = None) -> dict[str, Any]:
        value = normalize_source(source)
        if value is None:
            raise ValueError("source must be 'blob' or 'databricks'")
        previous = self.current()
        record = {"name": SETTING_NAME, "value": value, "previous": previous,
                  "updated_at": _now(), "updated_by": (actor or "unknown")[:200]}
        durable, error = False, None
        if self.store is not None:
            try:
                self.store.put_setting(SETTING_NAME, record)
                durable = True
            except Exception as exc:  # noqa: BLE001 - keep the switch, say it is not durable
                error = ds_log.error_text(exc, 500)
        if not durable:
            try:
                self._write_fallback(record)
            except Exception as exc:  # noqa: BLE001
                error = (error + "; " if error else "") + f"local fallback: {ds_log.error_text(exc, 120)}"
        with self._lock:
            self._record, self._origin = record, "switched"
            self._durable, self._persist_error = durable, error
            if self._load_state in {"not_loaded", "loading"}:
                self._load_state = "skipped"
        where = self._location() if durable else f"memory + {self.fallback_path} (NOT durable: {error})"
        ds_log.info(f"features source switched: {previous} -> {value} by {record['updated_by']} at {record['updated_at']} "
                    f"(persisted to {where}); results stay tagged with their scan source until a rescan")
        return self.describe()


def results_status(setting: dict[str, Any], snapshot: Mapping[str, Any] | None, scan_running: bool) -> dict[str, Any]:
    """Active source vs the source the current results were computed from."""
    snapshot = snapshot or {}
    scanned = bool(snapshot.get("scan_id")) and snapshot.get("scan_id") != "not-scanned"
    results_source = normalize_source(snapshot.get("features_source"))
    legacy = False
    if scanned and results_source is None:
        results_source, legacy = "blob", True  # every scan before this setting existed read blob
    active = setting.get("source")
    detail = snapshot.get("features_source_detail") or {}
    mismatch = bool(results_source) and results_source != active
    return {
        **setting,
        "active_source": active,
        "results_source": results_source,
        "results_source_label": SOURCE_LABELS.get(results_source or "", None),
        "results_scanned_at": detail.get("scanned_at") or (snapshot.get("generated_at") if scanned else None),
        "results_scan_id": snapshot.get("scan_id") if scanned or scan_running else None,
        "results_source_inferred": legacy,
        "mismatch": mismatch,
        "rescan_required": mismatch and not scan_running,
        "scan_running": scan_running,
        "notes": detail.get("notes") or ([] if active != "databricks" else [DATABRICKS_STALE_NOTE, SCHEMA_UNAVAILABLE_NOTE]),
    }


DATABRICKS_STALE_NOTE = (
    "The Databricks copy may be stale for older backfilled months: the bronze mirror only re-ingests the current "
    "and previous month, so blob files rewritten later are not reflected in feature_store."
)
SCHEMA_UNAVAILABLE_NOTE = (
    "Schema check unavailable in Databricks mode: feature_store has 23 canonical FST columns truly absent and "
    "collapses 11 case-duplicate column pairs, so per-month column completeness cannot be reproduced. Missing "
    "columns are not reported as gaps."
)
