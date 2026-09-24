from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from ..manifests import BackfillManifestWriter, MachineManifestRequest


@dataclass(frozen=True)
class WorkflowIdentity:
    workflow_name: str
    run_id: str | None = None


class ArgoWorkflowAdapter:
    def __init__(self, manifest_writer: BackfillManifestWriter) -> None:
        self.manifest_writer = manifest_writer
        self.namespace = os.getenv("BACKFILL_ARGO_NAMESPACE", "jobs-default")

    def submit(self, item: dict) -> WorkflowIdentity:
        expected = {"standard": "standard-8g-v1", "ulrpm": "ulrpm-64g-v1"}
        if item["cohort"] not in expected or item["resource_profile_version"] != expected[item["cohort"]]:
            raise ValueError("cohort/resource profile routing mismatch")
        template = os.getenv(f"BACKFILL_{item['cohort'].upper()}_WORKFLOW_TEMPLATE", "")
        if not template:
            raise RuntimeError(f"{item['cohort']} workflow template is not configured")
        manifest = self.manifest_writer.write_machine_manifest(MachineManifestRequest(
            machine_id=item["machine_id"], since=item["window_start"], until=item["window_end"]))
        name = self.external_name(item)
        command = ["argo", "submit", "-n", self.namespace, "--name", name, "--from", f"workflowtemplate/{template}",
                   "-l", f"backfill-campaign={item['campaign_id']}", "-p", "name-space=\"feature-store-container\"",
                   "-p", f"storage_account_manifest_path={json.dumps(manifest.manifest_path)}",
                   "-p", "manifest_bucket_count=1", "-p", "features_to_backfill=\"\"", "-o", "json"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=True)
        payload = json.loads(result.stdout)
        return WorkflowIdentity(payload.get("metadata", {}).get("name", name), payload.get("metadata", {}).get("uid"))

    @staticmethod
    def external_name(item: dict) -> str:
        return f"fstbf-{item['idempotency_key'][:24]}"

    def find(self, item: dict) -> WorkflowIdentity | None:
        name = self.external_name(item)
        result = subprocess.run(["argo", "get", "-n", self.namespace, name, "-o", "json"], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout)
        return WorkflowIdentity(payload.get("metadata", {}).get("name", name), payload.get("metadata", {}).get("uid"))

    def statuses(self) -> dict[str, str]:
        command = ["argo", "list", "-n", self.namespace, "-l", "backfill-campaign", "-o", "json"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=True)
        payload = json.loads(result.stdout)
        return {item["metadata"]["name"]: str(item.get("status", {}).get("phase", "")).lower() for item in payload.get("items", [])}

    def cancel(self, workflow_name: str) -> None:
        subprocess.run(["argo", "terminate", "-n", self.namespace, workflow_name], capture_output=True, text=True, timeout=30, check=True)
