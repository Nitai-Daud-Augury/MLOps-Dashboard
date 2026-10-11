"""Runtime modes and public, non-secret capability information.

Workflow mutations (trigger / terminate / logs / campaign actions) by runtime:

* ``local``      - always enabled; the argo/kubectl CLI backend (unchanged).
* ``databricks`` - enabled when the Outerbounds service principal (SP) is
  configured, i.e. both ``METAFLOW_SERVICE_AUTH_KEY`` and
  ``OBP_METAFLOW_CONFIG_URL`` are set (the same env keys the Outerbounds probe
  reads). The backend is the Metaflow client (see ``outerbounds_actions``).
  Without the SP the buttons stay visible and every mutation route answers
  HTTP 503 with ``workflow_mutations_error`` (never a silent no-op).
* ``docker``     - no workflow backend is packaged; mutations answer HTTP 403.

Only key *names* are ever reported, never values.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


RUNTIME_MODES = frozenset({"local", "docker", "databricks"})

# Env keys that make up the Outerbounds service-principal credential on Databricks.
OUTERBOUNDS_SP_ENV_KEYS = ("METAFLOW_SERVICE_AUTH_KEY", "OBP_METAFLOW_CONFIG_URL")
SP_NOT_CONFIGURED = "outerbounds_sp_not_configured"
NO_WORKFLOW_BACKEND = "runtime_without_workflow_backend"


class RuntimeConfigurationError(ValueError):
    """Raised when the configured dashboard runtime mode is invalid."""


@dataclass(frozen=True)
class RuntimeInfo:
    mode: str
    workflow_mutations: bool
    monitor: bool = True
    manifest_creation: bool = True
    # "argo_cli" (local), "metaflow" (Databricks Apps) or None (docker).
    workflow_backend: str | None = None
    # Why mutations are unavailable (None when they are available) and the
    # HTTP status the mutation routes answer with.
    workflow_mutations_error: str | None = None
    workflow_mutations_error_type: str | None = None
    workflow_mutations_status_code: int = 503

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "capabilities": {
                "monitor": self.monitor,
                "workflow_mutations": self.workflow_mutations,
                "manifest_creation": self.manifest_creation,
            },
            "workflow_backend": self.workflow_backend,
            "workflow_mutations_error": self.workflow_mutations_error,
            "workflow_mutations_error_type": self.workflow_mutations_error_type,
        }


def _values(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def resolve_runtime(environ: Mapping[str, str] | None = None) -> str:
    values = _values(environ)
    explicit = values.get("MLOPS_DASHBOARD_RUNTIME")
    if explicit is None:
        return "databricks" if "DATABRICKS_APP_PORT" in values else "local"
    mode = explicit.strip().lower()
    if mode not in RUNTIME_MODES:
        expected = ", ".join(sorted(RUNTIME_MODES))
        raise RuntimeConfigurationError(
            f"Invalid MLOPS_DASHBOARD_RUNTIME {explicit!r}; expected one of: {expected}"
        )
    return mode


def missing_outerbounds_sp_keys(environ: Mapping[str, str] | None = None) -> list[str]:
    values = _values(environ)
    return [key for key in OUTERBOUNDS_SP_ENV_KEYS if not str(values.get(key) or "").strip()]


def outerbounds_sp_configured(environ: Mapping[str, str] | None = None) -> bool:
    return not missing_outerbounds_sp_keys(environ)


def outerbounds_sp_missing_message(environ: Mapping[str, str] | None = None) -> str:
    missing = missing_outerbounds_sp_keys(environ) or list(OUTERBOUNDS_SP_ENV_KEYS)
    return (
        "Outerbounds service principal not configured: set METAFLOW_SERVICE_AUTH_KEY and "
        f"OBP_METAFLOW_CONFIG_URL (missing: {', '.join(missing)}). Bind them to the app from the "
        "Databricks secret scope (docs/DEPLOYMENT_RUNBOOK.md, 'Outerbounds service principal'), "
        "redeploy, then verify with Admin > Outerbounds probe."
    )


def runtime_info(environ: Mapping[str, str] | None = None) -> RuntimeInfo:
    mode = resolve_runtime(environ)
    if mode == "local":
        return RuntimeInfo(mode=mode, workflow_mutations=True, workflow_backend="argo_cli")
    if mode == "databricks":
        if outerbounds_sp_configured(environ):
            return RuntimeInfo(mode=mode, workflow_mutations=True, workflow_backend="metaflow")
        return RuntimeInfo(
            mode=mode,
            workflow_mutations=False,
            workflow_backend="metaflow",
            workflow_mutations_error=outerbounds_sp_missing_message(environ),
            workflow_mutations_error_type=SP_NOT_CONFIGURED,
            workflow_mutations_status_code=503,
        )
    return RuntimeInfo(
        mode=mode,
        workflow_mutations=False,
        workflow_backend=None,
        workflow_mutations_error=(
            f"Workflow operations are not available in the {mode} runtime: no workflow backend is "
            "packaged. Use the local dashboard or the Databricks app."
        ),
        workflow_mutations_error_type=NO_WORKFLOW_BACKEND,
        workflow_mutations_status_code=403,
    )


def workflow_mutations_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return runtime_info(environ).workflow_mutations
