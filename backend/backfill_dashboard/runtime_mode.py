"""Runtime modes and public, non-secret capability information.

Workflow mutations (trigger / terminate / logs / campaign actions) by runtime:

* ``local``      - always enabled; the argo/kubectl CLI backend (unchanged)
  unless ``OUTERBOUNDS_DISPATCH_BACKEND`` is set explicitly.
* ``databricks`` - enabled when the selected Outerbounds dispatch backend is
  configured. ``OUTERBOUNDS_DISPATCH_BACKEND`` picks it:

  - ``github_actions`` - actions run in the ``outerbounds-dispatch.yml`` GitHub
    Actions workflow (OIDC machine user); needs ``GITHUB_DISPATCH_TOKEN``
    (see ``github_dispatch`` / ``outerbounds_gha``).
  - ``metaflow`` - the in-app Metaflow client (``outerbounds_actions``); needs
    ``METAFLOW_SERVICE_AUTH_KEY`` + ``OBP_METAFLOW_CONFIG_URL``.
  - unset - ``metaflow`` when ``METAFLOW_SERVICE_AUTH_KEY`` is set, otherwise
    ``github_actions``.

  When the selected backend is not configured the buttons stay visible and
  every mutation route answers HTTP 503 with ``workflow_mutations_error``
  naming both options (never a silent no-op).
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
GITHUB_DISPATCH_NOT_CONFIGURED = "github_dispatch_not_configured"
INVALID_DISPATCH_BACKEND = "invalid_dispatch_backend"
DISPATCH_BACKEND_ENV = "OUTERBOUNDS_DISPATCH_BACKEND"
DISPATCH_BACKENDS = ("github_actions", "metaflow")
GITHUB_DISPATCH_TOKEN_ENV = "GITHUB_DISPATCH_TOKEN"


class RuntimeConfigurationError(ValueError):
    """Raised when the configured dashboard runtime mode is invalid."""


@dataclass(frozen=True)
class RuntimeInfo:
    mode: str
    workflow_mutations: bool
    monitor: bool = True
    manifest_creation: bool = True
    # "argo_cli" (local), "github_actions" / "metaflow" (Databricks Apps) or None (docker).
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
        "redeploy, then verify with Admin > Outerbounds probe. Alternatively set "
        f"{DISPATCH_BACKEND_ENV}=github_actions and {GITHUB_DISPATCH_TOKEN_ENV}."
    )


def github_dispatch_configured(environ: Mapping[str, str] | None = None) -> bool:
    return bool(str(_values(environ).get(GITHUB_DISPATCH_TOKEN_ENV) or "").strip())


def github_dispatch_missing_message(environ: Mapping[str, str] | None = None) -> str:
    return (
        f"Outerbounds actions are not configured. Option 1 (current, {DISPATCH_BACKEND_ENV}=github_actions): set "
        f"{GITHUB_DISPATCH_TOKEN_ENV} from the Databricks secret mlops-dashboard/github_dispatch_token (fine-grained "
        "PAT, Actions read/write + Contents read on the dispatch repository) - see docs/DEPLOYMENT_RUNBOOK.md, "
        "'Outerbounds via GitHub Actions'. Option 2 (metaflow): set METAFLOW_SERVICE_AUTH_KEY and "
        "OBP_METAFLOW_CONFIG_URL for an Outerbounds service principal. Redeploy, then verify with Admin > "
        "Outerbounds probe."
    )


def resolve_dispatch_backend(mode: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The workflow backend name for *mode* ("argo_cli", "metaflow", "github_actions", None), or
    ``"invalid:<value>"`` for an unknown OUTERBOUNDS_DISPATCH_BACKEND."""
    values = _values(environ)
    raw = str(values.get(DISPATCH_BACKEND_ENV) or "").strip().lower()
    if raw and raw not in DISPATCH_BACKENDS:
        return f"invalid:{raw[:40]}"
    if mode == "local":
        return raw or "argo_cli"
    if mode == "databricks":
        if raw:
            return raw
        return "metaflow" if str(values.get("METAFLOW_SERVICE_AUTH_KEY") or "").strip() else "github_actions"
    return None


def runtime_info(environ: Mapping[str, str] | None = None) -> RuntimeInfo:
    mode = resolve_runtime(environ)
    if mode in {"local", "databricks"}:
        backend = resolve_dispatch_backend(mode, environ)
        if backend and backend.startswith("invalid:"):
            return RuntimeInfo(
                mode=mode, workflow_mutations=False, workflow_backend=None,
                workflow_mutations_error=(
                    f"Invalid {DISPATCH_BACKEND_ENV} {backend[8:]!r}: expected github_actions or metaflow."
                ),
                workflow_mutations_error_type=INVALID_DISPATCH_BACKEND, workflow_mutations_status_code=503,
            )
        if backend == "argo_cli":
            return RuntimeInfo(mode=mode, workflow_mutations=True, workflow_backend="argo_cli")
        if backend == "metaflow":
            if outerbounds_sp_configured(environ):
                return RuntimeInfo(mode=mode, workflow_mutations=True, workflow_backend="metaflow")
            return RuntimeInfo(
                mode=mode, workflow_mutations=False, workflow_backend="metaflow",
                workflow_mutations_error=outerbounds_sp_missing_message(environ),
                workflow_mutations_error_type=SP_NOT_CONFIGURED, workflow_mutations_status_code=503,
            )
        if github_dispatch_configured(environ):
            return RuntimeInfo(mode=mode, workflow_mutations=True, workflow_backend="github_actions")
        return RuntimeInfo(
            mode=mode, workflow_mutations=False, workflow_backend="github_actions",
            workflow_mutations_error=github_dispatch_missing_message(environ),
            workflow_mutations_error_type=GITHUB_DISPATCH_NOT_CONFIGURED, workflow_mutations_status_code=503,
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
