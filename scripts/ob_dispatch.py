#!/usr/bin/env python3
"""Outerbounds action runner for the `outerbounds-dispatch` GitHub Actions workflow.

The Databricks App cannot hold an Outerbounds credential (static machine-user keys
are rejected), so it dispatches `.github/workflows/outerbounds-dispatch.yml`, which
authenticates the OIDC machine user `mlops-dashboard-sp` with
`outerbounds service-principal-configure --github-actions` and then runs this script.

Contract
--------
Inputs come ONLY from environment variables (the workflow maps each
`workflow_dispatch` input into `env:`; nothing is interpolated into a shell):

  OB_ACTION        trigger | status | terminate | list_runs
  OB_FLOW          flow name (allowlist below)
  OB_DEPLOYMENT_ID Argo WorkflowTemplate name(s) (trigger: one; terminate: comma list)
  OB_PARAMS_JSON   JSON object (trigger: flow parameters; list_runs: {"limit", "running_only"})
  OB_RUN_ID        argo-<name> or <name> (status / terminate)
  OB_REQUEST_ID    idempotency key, ^[A-Za-z0-9_-]{8,64}$ (also part of run-name)

Every value is validated here again: the workflow run-name and the dispatch
inputs are caller-controlled text.

Output: `<OB_RESULT_DIR>/result.json` (uploaded as artifact `ob-result-<request_id>`)
and a short Markdown table in $GITHUB_STEP_SUMMARY. Both are redacted and, by
default, PUBLIC-SAFE: no tokens, no config values, no env dumps, and no flow
parameter values (machine IDs, manifest paths) unless the repository variable
OB_DISPATCH_INCLUDE_PARAMS=1 (only for a private repository).

Exit code: 0 on success, 1 on a failed action, 2 on invalid input. result.json is
written in every case (`--ensure-result` writes a fallback when an earlier step
such as the Outerbounds login failed).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA_VERSION = 1
ACTIONS = ("trigger", "status", "terminate", "list_runs")
PROD_NAMESPACE = "feature-store-container"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
RUN_ID_RE = re.compile(r"^(?:argo-)?([a-z0-9][a-z0-9.-]{2,252})$")
DEPLOYMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,252}$")
MAX_PARAMS_JSON = 60_000
MAX_STRING = 32_768
MAX_LIST_LIMIT = 100

_STR, _INT, _BOOL = "str", "int", "bool"

# Flow -> allowed trigger parameters (Argo parameter names, exactly as the dashboard's
# argo-submit builders in backend/backfill_dashboard/admin.py emit them) and types.
FLOW_PARAMETERS: dict[str, dict[str, str]] = {
    "FSTBackfill": {
        "name-space": _STR,
        "storage_account_manifest_path": _STR,
        "manifest_bucket_count": _INT,
        "force_sessions_from_bucket": _BOOL,
        "features_to_backfill": _STR,
    },
    "UlrpmDevBackfillOrchestratorFlow": {
        "machine_ids": _STR,
        "name_space": _STR,
        "manifest_template": _STR,
        "start_index": _INT,
        "end_index": _INT,
        "machine_month_indices": _STR,
        "global_machine_concurrency": _INT,
        "child_max_parallel_steps": _INT,
        "batch_id": _STR,
        "orchestrator_contract_version": _STR,
        "redeploy_fst_template": _BOOL,
        "machine_manifest_plan": _STR,
    },
}
FLOWS = tuple(FLOW_PARAMETERS)
NAMESPACE_PARAMS = ("name-space", "name_space", "fst-namespace")
LIST_PARAMS = {"limit": _INT, "running_only": _BOOL}
# Same contract set as admin.ULRPM_ORCHESTRATOR_SUPPORTED_CONTRACTS.
ORCHESTRATOR_CONTRACT = "2026-10-06-v5"
ORCHESTRATOR_LEGACY_CONTRACTS = ("2026-09-17-v4",)


class InputError(ValueError):
    """Invalid dispatch input (exit 2)."""


# --- redaction -----------------------------------------------------------------------

_REDACTIONS = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)(x-api-key|authorization|api[_-]?key|auth[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)([^\s\"',;}&]+)"), r"\1\2<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"), "<redacted-jwt>"),
    (re.compile(r"(?i)(/v1/perimeters/[^/\s]+/)[^/\s\"']+"), r"\1<redacted>"),
    (re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"), "https://<redacted>@"),
    (re.compile(r"(?i)mongodb(?:\+srv)?://\S+"), "mongodb://<redacted>"),
)


def _secret_values() -> list[str]:
    values = [os.environ.get(name, "") for name in ("METAFLOW_SERVICE_AUTH_KEY", "OB_TERMINATE_AUTHORIZE_TOKEN",
                                                     "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "GITHUB_TOKEN")]
    home = os.environ.get("METAFLOW_HOME")
    if home:
        try:  # the key service-principal-configure wrote; read only to redact it
            config = json.loads((Path(home) / "config.json").read_text(encoding="utf-8"))
            values.append(str(config.get("METAFLOW_SERVICE_AUTH_KEY") or ""))
        except (OSError, ValueError, AttributeError):
            pass
    return [value for value in values if len(value) >= 6]


def redact(text: Any, secrets: list[str] | None = None) -> str:
    value = str(text)
    for secret in secrets if secrets is not None else _secret_values():
        value = value.replace(secret, "<redacted>")
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


def _clip(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else f"{text[:limit]}… [+{len(text) - limit} chars truncated]"


# --- input validation -------------------------------------------------------------------

def _typed(name: str, value: Any, kind: str) -> Any:
    if kind == _BOOL:
        if not isinstance(value, bool):
            raise InputError(f"parameter {name!r} must be a boolean")
    elif kind == _INT:
        if isinstance(value, bool) or not isinstance(value, int) or not (-1 <= value <= 1_000_000):
            raise InputError(f"parameter {name!r} must be an integer between -1 and 1000000")
    elif kind == _STR:
        if not isinstance(value, str):
            raise InputError(f"parameter {name!r} must be a string")
        if len(value) > MAX_STRING or any(ord(ch) < 32 and ch not in "\t" for ch in value):
            raise InputError(f"parameter {name!r} is too long or contains control characters")
    return value


def _parse_params(raw: str, allowed: Mapping[str, str]) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    if len(raw) > MAX_PARAMS_JSON:
        raise InputError(f"params_json is too large ({len(raw)} chars, max {MAX_PARAMS_JSON})")
    try:
        params = json.loads(raw)
    except ValueError as exc:
        raise InputError(f"params_json is not valid JSON: {exc.msg}") from None
    if not isinstance(params, dict):
        raise InputError("params_json must be a JSON object")
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise InputError(f"params_json has unsupported keys: {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")
    return {name: _typed(name, value, allowed[name]) for name, value in params.items()}


def _deployment_allowlist(environ: Mapping[str, str]) -> set[str]:
    return {item.strip() for item in str(environ.get("OB_DISPATCH_DEPLOYMENTS") or "").split(",") if item.strip()}


def _deployments(raw: str, environ: Mapping[str, str], *, single: bool) -> list[str]:
    items = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not items:
        raise InputError("deployment_id is required")
    if single and len(items) != 1:
        raise InputError("exactly one deployment_id is required")
    if len(items) > 5:
        raise InputError("at most 5 deployment ids")
    allow = _deployment_allowlist(environ)
    for item in items:
        if not DEPLOYMENT_RE.fullmatch(item):
            raise InputError("deployment_id has an invalid format")
        if allow and item not in allow:
            raise InputError(f"deployment_id {item!r} is not in the OB_DISPATCH_DEPLOYMENTS allowlist")
    return items


def validate(environ: Mapping[str, str]) -> dict[str, Any]:
    """Validated request from the OB_* environment variables. Raises InputError."""
    request_id = str(environ.get("OB_REQUEST_ID") or "").strip()
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise InputError("request_id must match ^[A-Za-z0-9_-]{8,64}$")
    action = str(environ.get("OB_ACTION") or "").strip()
    if action not in ACTIONS:
        raise InputError(f"action must be one of: {', '.join(ACTIONS)}")
    flow = str(environ.get("OB_FLOW") or "").strip()
    if flow and flow not in FLOWS:
        raise InputError(f"flow must be one of: {', '.join(FLOWS)}")
    run_raw = str(environ.get("OB_RUN_ID") or "").strip()
    request: dict[str, Any] = {"action": action, "request_id": request_id, "flow": flow or None}

    if action == "trigger":
        if not flow:
            raise InputError("trigger needs flow")
        request["deployment_ids"] = _deployments(environ.get("OB_DEPLOYMENT_ID", ""), environ, single=True)
        params = _parse_params(environ.get("OB_PARAMS_JSON", ""), FLOW_PARAMETERS[flow])
        namespaces = {str(params[name]).strip() for name in NAMESPACE_PARAMS if name in params}
        if PROD_NAMESPACE in namespaces and str(environ.get("OB_DISPATCH_ALLOW_PROD") or "") != "1":
            raise InputError(f"production namespace {PROD_NAMESPACE} is disabled for dispatch "
                             "(repository variable OB_DISPATCH_ALLOW_PROD is not 1)")
        request["params"] = params
    elif action in {"status", "terminate"}:
        match = RUN_ID_RE.fullmatch(run_raw)
        if not match:
            raise InputError("run_id must look like argo-<workflow name> (lowercase letters, digits, '.', '-')")
        request["workflow_name"] = match.group(1)
        if action == "terminate":
            request["deployment_ids"] = _deployments(environ.get("OB_DEPLOYMENT_ID", ""), environ, single=False)
        if (environ.get("OB_PARAMS_JSON") or "").strip() not in {"", "{}"}:
            raise InputError(f"{action} takes no params_json")
    else:  # list_runs
        params = _parse_params(environ.get("OB_PARAMS_JSON", ""), LIST_PARAMS)
        limit = int(params.get("limit", 20))
        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise InputError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        request["limit"] = limit
        request["running_only"] = bool(params.get("running_only", False))
    if action != "status" and action != "terminate" and run_raw:
        raise InputError(f"{action} takes no run_id")
    return request


# --- Metaflow helpers (imported lazily: only present on the dispatch runner) -----------------

def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _argo_client():
    from metaflow.metaflow_config import KUBERNETES_NAMESPACE
    from metaflow.plugins.argo.argo_client import ArgoClient

    return ArgoClient(namespace=KUBERNETES_NAMESPACE)


def _include_params(environ: Mapping[str, str]) -> bool:
    return str(environ.get("OB_DISPATCH_INCLUDE_PARAMS") or "") == "1"


def _workflow_entry(workflow: Mapping[str, Any], flow: str | None, include_params: bool) -> dict:
    metadata = workflow.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    status = workflow.get("status") or {}
    entry = {
        "metadata": {
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace"),
            "labels": {k: v for k, v in (metadata.get("labels") or {}).items()
                       if k.startswith("workflows.argoproj.io/") and len(str(v)) <= 256},
            "annotations": {k: annotations[k] for k in ("metaflow/flow_name", "metaflow/run_id", "metaflow/owner", "metaflow/branch_name")
                            if k in annotations},
            "creationTimestamp": metadata.get("creationTimestamp"),
        },
        "status": {"phase": status.get("phase"), "startedAt": status.get("startedAt"), "finishedAt": status.get("finishedAt")},
        "source": "argo",
    }
    if flow and "metaflow/flow_name" not in entry["metadata"]["annotations"]:
        entry["metadata"]["annotations"]["metaflow/flow_name"] = flow
    parameters = ((workflow.get("spec") or {}).get("arguments") or {}).get("parameters") or []
    if include_params:
        entry["spec"] = {"arguments": {"parameters": [
            {"name": item.get("name"), "value": _clip(str(item.get("value", "")), 8192)}
            for item in parameters if isinstance(item, dict)
        ]}}
    else:
        entry["parameter_names"] = sorted(str(item.get("name")) for item in parameters if isinstance(item, dict))
    return entry


def _run_entry(run: Any, flow: str) -> dict:
    run_id = str(run.id)
    phase = "Running" if not run.finished else ("Succeeded" if run.successful else "Failed")
    return {
        "metadata": {"name": run_id.removeprefix("argo-"),
                     "annotations": {"metaflow/flow_name": flow, "metaflow/run_id": run_id},
                     "creationTimestamp": _iso(run.created_at)},
        "status": {"phase": phase, "startedAt": _iso(run.created_at),
                   "finishedAt": _iso(getattr(run, "finished_at", None)) if run.finished else None},
        "source": "metaflow_run",
    }


def _template(deployment: str) -> dict:
    template = _argo_client().get_workflow_template(deployment)
    if template is None:
        raise LookupError(f"No deployed flow found for: {deployment}")
    return template


def _trigger_kwargs(params: Mapping[str, Any], param_info: Mapping[str, Any]) -> dict[str, Any]:
    """Argo parameter names -> DeployedFlow.trigger kwargs (as outerbounds_actions.trigger_kwargs)."""
    declared = {str(d["name"]): str(d.get("type") or "") for d in (param_info or {}).values()
                if isinstance(d, dict) and d.get("name")}
    kwargs: dict[str, Any] = {}
    for name, value in params.items():
        if declared and name not in declared:
            raise ValueError(f"deployment has no parameter {name!r} (declares: {', '.join(sorted(declared))})")
        kind = declared.get(name, "")
        if kind == "JSON" and isinstance(value, str):
            value = json.loads(value) if value else value
        elif kind == "int" and isinstance(value, str):
            value = int(value)
        elif kind == "float" and isinstance(value, (str, int)) and not isinstance(value, bool):
            value = float(value)
        elif kind == "bool" and isinstance(value, str):
            value = value.strip().lower() == "true"
        elif kind == "str" and not isinstance(value, str):
            value = json.dumps(value) if isinstance(value, (dict, list, bool)) or value is None else str(value)
        kwargs[name.replace("-", "_").lower()] = value
    return kwargs


def _check_orchestrator_contract(template: Mapping[str, Any], params: Mapping[str, Any]) -> None:
    deployed = ""
    for item in ((template.get("spec") or {}).get("arguments") or {}).get("parameters") or []:
        if isinstance(item, dict) and item.get("name") == "orchestrator_contract_version":
            deployed = str(item.get("value") or "").strip('"')
    if deployed == ORCHESTRATOR_CONTRACT:
        return
    if deployed in ORCHESTRATOR_LEGACY_CONTRACTS and not params.get("machine_manifest_plan"):
        return
    raise ValueError(
        f"ULRPM parent orchestrator template is stale or too old for this request: deployed contract "
        f"{deployed or 'none'}, expected {ORCHESTRATOR_CONTRACT}"
        f"{' (legacy ' + '/'.join(ORCHESTRATOR_LEGACY_CONTRACTS) + ' allows Full month only)' if deployed else ''}. "
        "Redeploy UlrpmDevBackfillOrchestratorFlow.py. Nothing was triggered."
    )


def op_trigger(request: Mapping[str, Any], environ: Mapping[str, str]) -> dict:
    from metaflow import DeployedFlow

    deployment = request["deployment_ids"][0]
    template = _template(deployment)
    annotations = (template.get("metadata") or {}).get("annotations") or {}
    deployed_flow = str(annotations.get("metaflow/flow_name") or "")
    if deployed_flow != request["flow"]:
        raise ValueError(f"deployment {deployment} deploys flow {deployed_flow or 'unknown'}, not {request['flow']}")
    if request["flow"] == "UlrpmDevBackfillOrchestratorFlow":
        _check_orchestrator_contract(template, request["params"])
    kwargs = _trigger_kwargs(request["params"], json.loads(annotations.get("metaflow/parameters") or "{}"))
    triggered = DeployedFlow.from_deployment(deployment).trigger(**kwargs)
    pathspec = str(getattr(triggered, "pathspec", "") or "")
    flow, _, run_id = pathspec.partition("/")
    if not run_id:
        raise RuntimeError(f"trigger of {deployment} returned no run id")
    return {"ok": True, "flow": flow, "deployment_id": deployment, "run_id": run_id,
            "workflow_id": run_id.removeprefix("argo-"), "parameter_names": sorted(kwargs)}


def _flows(request: Mapping[str, Any]) -> list[str]:
    return [request["flow"]] if request.get("flow") else list(FLOWS)


def op_status(request: Mapping[str, Any], environ: Mapping[str, str]) -> dict:
    from metaflow import Run
    from metaflow.exception import MetaflowNotFound

    name = request["workflow_name"]
    warnings: list[str] = []
    workflow = None
    try:
        found = _argo_client().get_workflow(name)
        if found is not None:
            workflow = _workflow_entry(found, request.get("flow"), _include_params(environ))
    except Exception as exc:  # noqa: BLE001 - fall back to the Metaflow run state
        warnings.append(f"argo phase unavailable: {type(exc).__name__}")
    if workflow is None:
        for flow in _flows(request):
            try:
                workflow = _run_entry(Run(f"{flow}/argo-{name}", _namespace_check=False), flow)
                break
            except MetaflowNotFound:
                continue
    if workflow is None:
        raise LookupError(f"Could not find workflow argo-{name} in Argo or in flows {', '.join(_flows(request))}")
    return {"ok": True, "workflow_id": name, "workflows": [workflow], "warnings": warnings}


def op_list_runs(request: Mapping[str, Any], environ: Mapping[str, str]) -> dict:
    import itertools

    from metaflow import Flow
    from metaflow.exception import MetaflowNotFound

    include = _include_params(environ)
    workflows, warnings, missing = [], [], []
    client, argo_ok = None, True
    for flow in _flows(request):
        try:
            runs = list(itertools.islice(Flow(flow).runs(), request["limit"]))
        except MetaflowNotFound:
            missing.append(flow)
            continue
        for run in runs:
            if request["running_only"] and run.finished:
                continue
            run_id = str(run.id)
            if not run.finished and run_id.startswith("argo-") and argo_ok:
                try:
                    client = client or _argo_client()
                    found = client.get_workflow(run_id[5:])
                    if found is not None:
                        workflows.append(_workflow_entry(found, flow, include))
                        continue
                except Exception as exc:  # noqa: BLE001
                    argo_ok = False
                    warnings.append(f"argo phase unavailable ({type(exc).__name__}); using Metaflow run state")
            workflows.append(_run_entry(run, flow))
    return {"ok": True, "workflows": workflows, "warnings": warnings, "flows_not_found": missing,
            "parameters_included": include}


def op_terminate(request: Mapping[str, Any], environ: Mapping[str, str]) -> dict:
    from metaflow import DeployedFlow
    from metaflow.plugins.argo.argo_workflows import ArgoWorkflows

    name = request["workflow_name"]
    execution = ArgoWorkflows.get_execution(name)
    if execution is None:
        raise LookupError(f"Could not find workflow {name} on Argo Workflows")
    flow_name = execution[2]
    if request.get("flow") and request["flow"] != flow_name:
        raise ValueError(f"run argo-{name} belongs to {flow_name}, not {request['flow']}")
    if flow_name not in FLOWS:
        raise ValueError(f"run argo-{name} belongs to {flow_name}, which is not an allowlisted flow")
    token = str(environ.get("OB_TERMINATE_AUTHORIZE_TOKEN") or "").strip()
    checked = []
    for deployment in request["deployment_ids"]:
        deployed = DeployedFlow.from_deployment(deployment)
        checked.append(deployment)
        if deployed.deployer.flow_name != flow_name:
            continue
        triggered = DeployedFlow.get_triggered_run(deployment, f"argo-{name}")
        if not triggered.terminate(**({"authorize": token} if token else {})):
            raise RuntimeError(
                f"Metaflow argo-workflows terminate failed for {flow_name}/argo-{name}. A deployment created by "
                "another user needs its production token: add repository secret OB_TERMINATE_AUTHORIZE_TOKEN, "
                "or stop the run from the Outerbounds UI."
            )
        return {"ok": True, "workflow_id": name, "flow": flow_name, "deployment_id": deployment}
    raise LookupError(f"Run argo-{name} ({flow_name}) matches none of the deployments: {', '.join(checked)}")


OPS: dict[str, Callable[[Mapping[str, Any], Mapping[str, str]], dict]] = {
    "trigger": op_trigger,
    "status": op_status,
    "list_runs": op_list_runs,
    "terminate": op_terminate,
}


# --- output ----------------------------------------------------------------------------------

def _exc_type(exc: BaseException) -> str:
    module = type(exc).__module__
    return type(exc).__name__ if module in ("builtins", "__main__") else f"{module}.{type(exc).__name__}"


def _envelope(environ: Mapping[str, str], request_id: str, action: str) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "request_id": request_id if REQUEST_ID_RE.fullmatch(request_id or "") else None,
        "action": action if action in ACTIONS else None,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "github_run_id": environ.get("GITHUB_RUN_ID") if str(environ.get("GITHUB_RUN_ID") or "").isdigit() else None,
    }


def _result_dir(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("OB_RESULT_DIR") or "ob-result")


def write_outputs(result: dict, environ: Mapping[str, str]) -> None:
    secrets = _secret_values() + hidden_values(environ)
    text = redact(json.dumps(result, sort_keys=True, default=str), secrets)
    directory = _result_dir(environ)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.json").write_text(text + "\n", encoding="utf-8")
    summary = environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        safe = json.loads(text)
        rows = [("action", safe.get("action")), ("request_id", safe.get("request_id")), ("ok", safe.get("ok"))]
        for key in ("flow", "run_id", "workflow_id", "deployment_id", "error_type"):
            if safe.get(key):
                rows.append((key, safe[key]))
        if isinstance(safe.get("workflows"), list):
            rows.append(("workflows", len(safe["workflows"])))
        if safe.get("error"):
            rows.append(("error", _clip(str(safe["error"]), 500)))
        lines = ["### Outerbounds dispatch result", "", "| field | value |", "| --- | --- |"]
        lines += [f"| {key} | {str(value).replace('|', '/').replace(chr(10), ' ')} |" for key, value in rows]
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _value_pieces(raw: str) -> list[str]:
    """String values inside params_json (and their comma-separated parts), longest first."""
    try:
        params = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return []
    pieces: set[str] = set()
    for value in (params.values() if isinstance(params, dict) else []):
        if isinstance(value, str):
            pieces.add(value)
            pieces.update(part.strip() for part in value.split(","))
    return sorted((item for item in pieces if len(item) >= 8), key=len, reverse=True)


def hidden_values(environ: Mapping[str, str]) -> list[str]:
    """Parameter values kept out of public output (unless OB_DISPATCH_INCLUDE_PARAMS=1)."""
    if _include_params(environ):
        return []
    raw = str(environ.get("OB_PARAMS_JSON") or "")
    return ([raw] if len(raw) >= 8 else []) + _value_pieces(raw)


def mask_inputs(environ: Mapping[str, str]) -> int:
    """`--mask-inputs`: register GitHub log masks for parameter values before the dispatch
    step prints its env block. Reads the event payload file, never the shell."""
    path = environ.get("GITHUB_EVENT_PATH")
    try:
        inputs = (json.loads(Path(path).read_text(encoding="utf-8")).get("inputs") or {}) if path else {}
    except (OSError, ValueError, AttributeError):
        inputs = {}
    raw = str(inputs.get("params_json") or "")
    for value in ([raw] if len(raw) >= 8 else []) + _value_pieces(raw):
        if "\n" not in value and "\r" not in value:
            print(f"::add-mask::{value}", flush=True)
    return 0


def run(environ: Mapping[str, str]) -> tuple[int, dict]:
    envelope = _envelope(environ, str(environ.get("OB_REQUEST_ID") or ""), str(environ.get("OB_ACTION") or ""))
    hidden = hidden_values(environ)

    def scrub(text: str) -> str:
        text = redact(text)
        for value in hidden:
            text = text.replace(value, "<param>")
        return text

    try:
        request = validate(environ)
    except InputError as exc:
        return 2, {**envelope, "ok": False, "error_type": "invalid_input", "error": scrub(str(exc))}
    started = time.monotonic()
    try:
        from metaflow import namespace

        namespace(None)  # read any user's runs: the machine user authored none of them
        result = OPS[request["action"]](request, environ)
    except BaseException as exc:  # noqa: BLE001 - reported in result.json
        return 1, {**envelope, "ok": False, "error_type": _exc_type(exc), "error": _clip(scrub(str(exc)), 2000),
                   "ms": int((time.monotonic() - started) * 1000)}
    return 0, {**envelope, **result, "ms": int((time.monotonic() - started) * 1000)}


def ensure_result(environ: Mapping[str, str]) -> int:
    """`--ensure-result`: fallback result.json when the dispatch step never wrote one."""
    if (_result_dir(environ) / "result.json").is_file():
        return 0
    envelope = _envelope(environ, str(environ.get("OB_REQUEST_ID") or ""), str(environ.get("OB_ACTION") or ""))
    status = str(environ.get("OB_JOB_STATUS") or "unknown")
    write_outputs({**envelope, "ok": False, "error_type": "workflow_setup_failed",
                   "error": f"The dispatch workflow failed before the action ran (job status: {status}). "
                            "Most often the Outerbounds login (service-principal-configure) was rejected: check the "
                            "machine user's OIDC claims and perimeter grant. See the workflow run log."}, environ)
    return 0


def main(argv: list[str]) -> int:
    environ = dict(os.environ)
    if argv[:1] == ["--ensure-result"]:
        return ensure_result(environ)
    if argv[:1] == ["--mask-inputs"]:
        return mask_inputs(environ)
    if argv:
        print("usage: ob_dispatch.py [--mask-inputs | --ensure-result]  (inputs come from OB_* environment variables)",
              file=sys.stderr)
        return 2
    code, result = run(environ)
    write_outputs(result, environ)
    print(f"ob_dispatch: action={result.get('action')} request_id={result.get('request_id')} ok={result.get('ok')}"
          f"{' error_type=' + str(result.get('error_type')) if not result.get('ok') else ''}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
