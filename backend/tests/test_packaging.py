from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml


DASHBOARD_ROOT = Path(__file__).resolve().parents[2]


def _run_module():
    spec = importlib.util.spec_from_file_location("mlops_dashboard_run", DASHBOARD_ROOT / "run.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_databricks_app_manifest_uses_safe_same_origin_runtime():
    manifest = yaml.safe_load((DASHBOARD_ROOT / "app.yaml").read_text(encoding="utf-8"))
    assert manifest["command"] == ["python", "run.py"]
    environment = {item["name"]: item for item in manifest["env"]}
    assert environment["MLOPS_DASHBOARD_RUNTIME"]["value"] == "databricks"
    assert environment["BACKFILL_DISPATCH_ENABLED"]["value"] == "0"
    assert environment["BACKFILL_PRODUCTION_MODE"]["value"] == "0"
    assert environment["ULRPM_MACHINE_IDS_FILE"]["value"] == "data/unique_machine_ids.txt"
    assert environment["MONGODB_URL"]["valueFrom"] == "mongodb_url"
    assert environment["FST_PROD_ACCOUNT_NAME"]["value"] == "auguryprodfsthns"
    assert environment["FST_PROD_CONTAINER"]["value"] == "feature-store-container"
    assert environment["FST_PROD_ACCOUNT_KEY"]["valueFrom"] == "fst_account_key"
    assert "value" not in environment["FST_PROD_ACCOUNT_KEY"]
    assert all(("value" in item) != ("valueFrom" in item) for item in manifest["env"])
    values = [entry.get("value", "") for entry in environment.values()]
    assert not any(re.search(r"https?://|[0-9a-f]{24}", value, re.I) for value in values)


def test_databricks_bundle_names_sources_targets_and_secret_binding_are_safe():
    bundle = yaml.safe_load((DASHBOARD_ROOT / "databricks.yml").read_text(encoding="utf-8"))
    resource = yaml.safe_load((DASHBOARD_ROOT / "resources" / "mlops_dashboard.app.yml").read_text(encoding="utf-8"))
    assert bundle["bundle"]["name"] == "mlops-dashboard"
    assert set(bundle["targets"]) == {"dev", "prod"}
    assert bundle["targets"]["dev"]["variables"]["app_name"] == "mlops-dashboard-dev"
    assert bundle["targets"]["prod"]["variables"]["app_name"] == "mlops-dashboard"
    assert all("workspace" not in target or "host" not in target["workspace"] for target in bundle["targets"].values())
    assert all("profile" not in target.get("workspace", {}) for target in bundle["targets"].values())
    app = resource["resources"]["apps"]["mlops_dashboard"]
    assert app["name"] == "${var.app_name}"
    assert app["description"] == "MLOps Dashboard"
    assert app["source_code_path"] == ".."
    assert app["lifecycle"]["started"] is False
    assert app["resources"] == [
        {"name": "mongodb_url", "secret": {
            "scope": "${var.mongodb_secret_scope}",
            "key": "${var.mongodb_secret_key}",
            "permission": "READ",
        }},
        {"name": "fst_account_key", "secret": {
            "scope": "${var.mongodb_secret_scope}",
            "key": "fst_prod_account_key",
            "permission": "READ",
        }},
    ]
    assert {"mongodb_secret_scope", "mongodb_secret_key"} <= set(bundle["variables"])
    serialized = (DASHBOARD_ROOT / "databricks.yml").read_text(encoding="utf-8") + (DASHBOARD_ROOT / "resources" / "mlops_dashboard.app.yml").read_text(encoding="utf-8")
    assert not re.search(r"https?://|/Users/(?!\$\{workspace\.current_user\.userName\})[^\s/]+/|[0-9a-f]{24}|password\s*:\s*\S+|token\s*:\s*\S+", serialized, re.I)


def test_bundle_sync_excludes_local_runtime_and_research_content_but_keeps_sources():
    bundle = yaml.safe_load((DASHBOARD_ROOT / "databricks.yml").read_text(encoding="utf-8"))
    excludes = set(bundle["sync"]["exclude"])
    for pattern in (".env", ".env.*", "node_modules/**", "dist/**", "**/__pycache__/**",
                    "**/.pytest_cache/**", "**/*.sqlite3", "**/*state*.json", "MLOps research/**", "Augury repos/**"):
        assert pattern in excludes
    for path in ("app.yaml", "requirements.txt", "data/unique_machine_ids.txt", "backend", "src", "package-lock.json"):
        assert (DASHBOARD_ROOT / path).exists()


def test_github_workflows_are_pr_safe_oauth_m2m_poc_and_cost_conservative():
    workflow_root = DASHBOARD_ROOT / ".github" / "workflows"
    ci = yaml.load((workflow_root / "ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    deploy = yaml.load((workflow_root / "deploy.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    control = yaml.load((workflow_root / "app-control.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert set(ci["on"]) == {"pull_request", "push"}
    assert ci["on"]["push"]["branches"] == ["main"]
    assert ci["permissions"] == {"contents": "read"}
    assert ci["concurrency"]["cancel-in-progress"] == "true"
    ci_steps = [step for job in ci["jobs"].values() for step in job["steps"]]
    assert any(step.get("with", {}).get("node-version") == "22" for step in ci_steps)
    assert any(step.get("run") == "npm ci" for step in ci_steps)
    assert any(step.get("run") == "npm run lint" for step in ci_steps)
    assert any(step.get("run") == "npm run build" for step in ci_steps)
    assert any("python-version" in step.get("with", {}) and step["with"]["python-version"] == "3.11" for step in ci_steps)
    assert any("pip install -r requirements.txt pytest" in step.get("run", "") for step in ci_steps)
    assert any("pytest -q tests" in step.get("run", "") for step in ci_steps)

    assert set(deploy["on"]) == {"push", "workflow_dispatch"}
    assert "pull_request" not in deploy["on"]
    assert deploy["on"]["push"]["branches"] == ["main"]
    assert deploy["permissions"] == {"contents": "read", "id-token": "write"}
    assert deploy["jobs"]["deploy"]["needs"] == "verify"
    assert deploy["jobs"]["verify"]["permissions"] == {"contents": "read"}
    verify_steps = deploy["jobs"]["verify"]["steps"]
    assert any(step.get("run") == "npm run lint" for step in verify_steps)
    assert any(step.get("run") == "npm run build" for step in verify_steps)
    assert any("pytest -q tests" in step.get("run", "") for step in verify_steps)
    deploy_job = deploy["jobs"]["deploy"]
    assert "databricks-prod" in deploy_job["environment"]["name"]
    assert "databricks-dev" in deploy_job["environment"]["name"]
    # POC temporary: oauth-m2m plus a GitHub Environment secret reference.
    # github-oidc is the long-term target. Revert these assertions to github-oidc
    # and drop CLIENT_SECRET when account-admin federation lands.
    assert deploy_job["env"]["DATABRICKS_AUTH_TYPE"] == "oauth-m2m"
    assert deploy_job["env"]["DATABRICKS_HOST"] == "${{ vars.DATABRICKS_HOST }}"
    assert deploy_job["env"]["DATABRICKS_CLIENT_ID"] == "${{ vars.DATABRICKS_CLIENT_ID }}"
    assert deploy_job["env"]["DATABRICKS_CLIENT_SECRET"] == "${{ secrets.DATABRICKS_CLIENT_SECRET }}"
    assert deploy_job["env"]["BUNDLE_VAR_mongodb_secret_scope"] == "${{ vars.MONGODB_SECRET_SCOPE }}"
    assert deploy_job["env"]["BUNDLE_VAR_mongodb_secret_key"] == "${{ vars.MONGODB_SECRET_KEY }}"
    dispatch_inputs = deploy["on"]["workflow_dispatch"]["inputs"]
    assert dispatch_inputs["target"]["type"] == "choice"
    assert dispatch_inputs["target"]["options"] == ["dev", "prod"]
    assert dispatch_inputs["keep_running"]["type"] == "boolean"
    assert dispatch_inputs["keep_running"]["default"] == "false"
    deploy_steps = deploy_job["steps"]
    assert any("databricks/setup-cli@" in step.get("uses", "") for step in deploy_steps)
    for command in (
        "databricks bundle validate --target \"$TARGET\"",
        "databricks bundle deploy --target \"$TARGET\"",
        "databricks bundle run mlops_dashboard --target \"$TARGET\"",
    ):
        assert any(step.get("run") == command for step in deploy_steps)
    assert any("RUNNING" in step.get("run", "") and "databricks apps get" in step.get("run", "") for step in deploy_steps)
    cleanup = [step for step in deploy_steps if "databricks apps stop" in step.get("run", "")]
    assert len(cleanup) == 1
    assert "always()" in cleanup[0]["if"] and "keep_running" in cleanup[0]["if"] and "failure()" in cleanup[0]["if"]
    target_script = next(step["run"] for step in deploy_steps if step.get("id") == "target")
    assert "mlops-dashboard-dev" in target_script and "mlops-dashboard" in target_script
    assert "case \"$EVENT_NAME:$REQUESTED_TARGET\"" in target_script
    assert "refs/heads/main" in target_script

    assert set(control["on"]) == {"workflow_dispatch"}
    assert control["permissions"] == {"contents": "read", "id-token": "write"}
    control_inputs = control["on"]["workflow_dispatch"]["inputs"]
    assert control_inputs["target"]["type"] == "choice" and control_inputs["target"]["options"] == ["dev", "prod"]
    assert control_inputs["action"]["type"] == "choice" and control_inputs["action"]["options"] == ["status", "start", "stop"]
    assert "databricks-prod" in control["jobs"]["control"]["environment"]["name"]
    control_job = control["jobs"]["control"]
    # Same POC auth as deploy. Revert to github-oidc and drop CLIENT_SECRET
    # when account-admin federation lands.
    assert control_job["env"]["DATABRICKS_AUTH_TYPE"] == "oauth-m2m"
    assert control_job["env"]["DATABRICKS_HOST"] == "${{ vars.DATABRICKS_HOST }}"
    assert control_job["env"]["DATABRICKS_CLIENT_ID"] == "${{ vars.DATABRICKS_CLIENT_ID }}"
    assert control_job["env"]["DATABRICKS_CLIENT_SECRET"] == "${{ secrets.DATABRICKS_CLIENT_SECRET }}"
    control_steps = control["jobs"]["control"]["steps"]
    assert any("databricks apps get" in step.get("run", "") for step in control_steps)
    assert any("databricks apps start" in step.get("run", "") for step in control_steps)
    assert any("databricks apps stop" in step.get("run", "") for step in control_steps)
    control_target_script = next(step["run"] for step in control_steps if step.get("id") == "target")
    assert "mlops-dashboard-dev" in control_target_script and "mlops-dashboard" in control_target_script

    all_workflows = "\n".join(path.read_text(encoding="utf-8") for path in workflow_root.glob("*.yml"))
    # The POC references the Environment secret name only. No secret values belong in the repo.
    assert set(re.findall(r"secrets\.[A-Za-z0-9_]+", all_workflows)) == {"secrets.DATABRICKS_CLIENT_SECRET"}
    assert "DATABRICKS_TOKEN" not in all_workflows
    assert "BUNDLE_VAR_mongodb_secret_scope" in all_workflows
    assert "BUNDLE_VAR_mongodb_secret_key" in all_workflows
    assert "databricks/setup-cli@v1.10.0" in all_workflows
    assert "@main" not in all_workflows
    assert not re.search(r"https?://|/Users/ndaud/|[0-9a-f]{24}", all_workflows, re.I)


def test_dockerfile_builds_frontend_and_runs_safe_nonroot_api():
    dockerfile = (DASHBOARD_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM node:22-alpine AS frontend-build" in dockerfile
    assert "RUN npm ci" in dockerfile and "RUN npm run build" in dockerfile
    assert "FROM python:3.11-slim AS runtime" in dockerfile
    assert "COPY --from=frontend-build /app/dist ./dist" in dockerfile
    assert "MLOPS_DASHBOARD_RUNTIME=docker" in dockerfile
    assert "BACKFILL_DISPATCH_ENABLED=0" in dockerfile
    assert "BACKFILL_PRODUCTION_MODE=0" in dockerfile
    assert "USER dashboard" in dockerfile
    assert "HEALTHCHECK" in dockerfile and '"python", "-c"' in dockerfile
    assert 'CMD ["python", "run.py"]' in dockerfile


def test_root_python_requirements_reuse_backend_dependencies():
    root_requirements = (DASHBOARD_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert root_requirements == ["-r backend/requirements.txt"]
    backend_requirements = (DASHBOARD_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    assert "fastapi" in backend_requirements and "uvicorn" in backend_requirements


def test_packaged_machine_cohort_is_curated_and_in_repo(monkeypatch):
    from backfill_dashboard.config import _machine_ids_path

    monkeypatch.delenv("ULRPM_MACHINE_IDS_FILE", raising=False)
    path = _machine_ids_path()
    assert path == DASHBOARD_ROOT / "data" / "unique_machine_ids.txt"
    machine_ids = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(machine_ids) == 42
    assert len(set(machine_ids)) == 42
    assert all(re.fullmatch(r"[0-9a-f]{24}", machine_id) for machine_id in machine_ids)

    monkeypatch.setenv("ULRPM_MACHINE_IDS_FILE", "custom/cohort.txt")
    assert _machine_ids_path() == DASHBOARD_ROOT / "custom" / "cohort.txt"


def test_entrypoint_port_precedence_and_validation():
    resolve_port = _run_module().resolve_port
    assert resolve_port({}) == 8000
    assert resolve_port({"PORT": "9000"}) == 9000
    assert resolve_port({"DATABRICKS_APP_PORT": "8081", "PORT": "9000"}) == 8081
    for environ in ({"PORT": "not-a-port"}, {"PORT": "0"}, {"PORT": "65536"}):
        try:
            resolve_port(environ)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid port was accepted: {environ}")


def test_ignore_files_exclude_runtime_artifacts_not_deployment_manifests():
    gitignore = (DASHBOARD_ROOT / ".gitignore").read_text(encoding="utf-8")
    dockerignore = (DASHBOARD_ROOT / ".dockerignore").read_text(encoding="utf-8")
    for pattern in ("node_modules/", "dist/", "*.sqlite3", "__pycache__/"):
        assert pattern in gitignore
    for pattern in (".env", "node_modules", ".pytest_cache", "*.sqlite3", "Augury repos"):
        assert pattern in dockerignore
    for manifest in ("app.yaml", "Dockerfile", "requirements.txt"):
        assert (DASHBOARD_ROOT / manifest).is_file()


def test_team_docs_and_plan_are_shareable_and_match_runtime():
    readme = (DASHBOARD_ROOT / "README.md").read_text(encoding="utf-8")
    runbook = (DASHBOARD_ROOT / "docs" / "DEPLOYMENT_RUNBOOK.md").read_text(encoding="utf-8")
    package = yaml.safe_load((DASHBOARD_ROOT / "package.json").read_text(encoding="utf-8"))
    for heading in ("Data and coverage rules", "Architecture and runtime limits", "Repository layout",
                    "Prerequisites", "Local setup and development", "Docker", "Deploy and operate"):
        assert heading in readme
    for heading in ("One-time GitHub and Databricks setup", "Bundle commands", "Workflow behavior",
                    "Preferred lifecycle operations", "Cost controls", "Post-deployment verification",
                    "Troubleshooting", "Rollback and release ownership", "Not yet supported"):
        assert heading in runbook
    assert "Ctrl-C" in readme and "docker stop mlops-dashboard" in readme
    assert "BUNDLE_VAR_mongodb_secret_scope" in runbook
    assert "BUNDLE_VAR_mongodb_secret_key" in runbook
    assert "MONGODB_SECRET_SCOPE" in runbook and "MONGODB_SECRET_KEY" in runbook
    assert "Refresh/rotate Mongo auth only when status reports unauthorized/unavailable/expired" in runbook
    assert "snapshot.lifecycle_status" not in runbook
    assert "valueFrom" in (DASHBOARD_ROOT / "app.yaml").read_text(encoding="utf-8")
    assert package["scripts"]["api"].startswith("python -m uvicorn ")
    assert ".venv/bin" not in package["scripts"]["api"]
    assert "databricks-prod" in runbook and "app-control.yml" in runbook
    combined = readme + runbook
    assert "mongodb+srv://" not in combined
    assert "ghp_" not in combined and "dapi" not in combined
    assert "/Users/ndaud/" not in combined
