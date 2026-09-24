"""Runtime modes and public, non-secret capability information."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


RUNTIME_MODES = frozenset({"local", "docker", "databricks"})


class RuntimeConfigurationError(ValueError):
    """Raised when the configured dashboard runtime mode is invalid."""


@dataclass(frozen=True)
class RuntimeInfo:
    mode: str
    workflow_mutations: bool
    monitor: bool = True
    manifest_creation: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "capabilities": {
                "monitor": self.monitor,
                "workflow_mutations": self.workflow_mutations,
                "manifest_creation": self.manifest_creation,
            },
        }


def resolve_runtime(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
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


def runtime_info(environ: Mapping[str, str] | None = None) -> RuntimeInfo:
    mode = resolve_runtime(environ)
    return RuntimeInfo(mode=mode, workflow_mutations=mode == "local")


def workflow_mutations_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return runtime_info(environ).workflow_mutations
