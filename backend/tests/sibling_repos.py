"""Skip tests that read sibling Augury checkouts.

Those repos sit next to this dashboard in the local MLOps onboarding layout.
GitHub Actions checks out only this repository, and the siblings are not vendored.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# backend/tests/this file -> parents[3] is the directory that contains this repo.
WORKSPACE_PARENT = Path(__file__).resolve().parents[3]
AUGURY_REPOS = WORKSPACE_PARENT / "Augury repos"

METAFLOW_FLOW = AUGURY_REPOS / "metaflow-bx" / "FSTBackfill_prod_flow.py"
ULRPM_ORCHESTRATOR = (
    AUGURY_REPOS
    / "MLOps research"
    / "ulrpm_dev_backfill"
    / "UlrpmDevBackfillOrchestratorFlow.py"
)
CANONICAL_CLASSIFIER_REPO = AUGURY_REPOS / "machine-sample-fetcher"

_ABSENT = (
    "Sibling Augury checkout is not in this repo. "
    "Skipped because GitHub Actions only checks out MLOps-Dashboard."
)


def _canonical_classifier_available() -> bool:
    try:
        import machine_sample_fetcher.hardware_classification  # noqa: F401
    except ImportError:
        return CANONICAL_CLASSIFIER_REPO.is_dir()
    return True


requires_metaflow_flow = pytest.mark.skipif(
    not METAFLOW_FLOW.is_file(),
    reason=f"{_ABSENT} Missing {METAFLOW_FLOW}.",
)
requires_ulrpm_orchestrator = pytest.mark.skipif(
    not ULRPM_ORCHESTRATOR.is_file(),
    reason=f"{_ABSENT} Missing {ULRPM_ORCHESTRATOR}.",
)
requires_flow_sources = pytest.mark.skipif(
    not METAFLOW_FLOW.is_file() or not ULRPM_ORCHESTRATOR.is_file(),
    reason=f"{_ABSENT} Missing the metaflow-bx flow and/or the ULRPM orchestrator.",
)
requires_canonical_classifier = pytest.mark.skipif(
    not _canonical_classifier_available(),
    reason=(
        f"{_ABSENT} Hardware cohort classification needs "
        "Augury repos/machine-sample-fetcher. Without it, classify_machine fails closed to unknown."
    ),
)
