"""Fail-closed machine resource classification.

This module intentionally accepts normalized dictionaries so it can be used by
the Mongo adapter, file fixtures, and the production classifier parity tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import importlib
import sys
from typing import Any, Callable, Literal

ResourceCohort = Literal["standard", "ulrpm", "unknown"]
CLASSIFIER_VERSION = "endpoint-brand-v2"


@dataclass(frozen=True)
class ClassificationResult:
    cohort: ResourceCohort
    reasons: tuple[str, ...]
    classifier_version: str = CLASSIFIER_VERSION


def _hardware_types(document: dict[str, Any]) -> tuple[str, ...] | None:
    endpoints = document.get("endpoints")
    if endpoints is None:
        return None
    if not isinstance(endpoints, list):
        return None
    values: list[str] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            return None
        value = endpoint.get("hardware_type", endpoint.get("type"))
        if not isinstance(value, str) or not value.strip():
            return None
        values.append(value.strip())
    return tuple(values)


def classify_machine(document: dict[str, Any]) -> ClassificationResult:
    if not isinstance(document, dict):
        return ClassificationResult("unknown", ("malformed_machine_document",))
    tags = document.get("tags", ())
    if tags is None:
        tags = ()
    if not isinstance(tags, (list, tuple, set)):
        return ClassificationResult("unknown", ("malformed_tags",))
    hardware = _hardware_types(document)
    # A positive tag is sufficient evidence, even when endpoint metadata is
    # absent. Missing endpoint data is unknown only when no positive evidence
    # exists; it must never downgrade a tagged ULRPM machine.
    positive_tag = any(str(tag).strip().lower() == "ulrpm" for tag in tags)
    if hardware is None:
        return ClassificationResult("ulrpm", ("ulrpm_tag",)) if positive_tag else ClassificationResult("unknown", ("missing_or_malformed_endpoint_data",))
    try:
        classifier = _shared_classifier()
        result = classifier(tags, hardware)
        return ClassificationResult(result.cohort, tuple(result.reasons), result.classifier_version)
    except Exception:
        # No local truth table: a missing/broken canonical classifier is
        # uncertainty and therefore must fail closed.
        return ClassificationResult("ulrpm", ("ulrpm_tag",)) if positive_tag else ClassificationResult("unknown", ("canonical_classifier_unavailable",))


def _shared_classifier() -> Callable:
    try:
        from machine_sample_fetcher.hardware_classification import classify_resource
        return classify_resource
    except ImportError:
        root = Path(__file__).resolve().parents[3]
        repository = root / "Augury repos" / "machine-sample-fetcher"
        if not repository.exists():
            raise ImportError("canonical machine classifier is unavailable")
        sys.path.insert(0, str(repository))
        try:
            package = importlib.import_module("machine_sample_fetcher")
            local_package = str(repository / "machine_sample_fetcher")
            if local_package not in package.__path__:
                package.__path__.insert(0, local_package)
            return importlib.import_module("machine_sample_fetcher.hardware_classification").classify_resource
        finally:
            sys.path.remove(str(repository))
