import type { EnvironmentTarget, SourceType } from "./admin.types";
export interface WorkflowSourceForm {
  source_type: SourceType;
  local_flow_path: string;
  github_repo_url: string;
  github_ref: string;
  github_flow_path: string;
}
export interface TriggerForm {
  environment: EnvironmentTarget;
  namespace: string;
  storage_account_manifest_path: string;
  include_features_to_backfill: boolean;
  features_to_backfill: string;
  max_parallel_steps: number;
  force_sessions_from_bucket: boolean;
  confirm_production: boolean;
  confirmation_text: string;
}
export interface LogsForm {
  run_task_path: string;
  stream: "stdout" | "stderr";
}
export interface MachineManifestForm {
  machine_id: string;
  since: string;
  until: string;
  manifest_path: string;
}
export interface MultiMachineManifestForm {
  machine_ids: string;
  since: string;
  until: string;
  manifest_path: string;
}
export interface AllMachinesBackfillForm {
  since: string;
  until: string;
  manifest_path: string;
}
export interface ManifestResult {
  account_name: string;
  container_name: string;
  manifest_path: string;
  manifest_prefix?: string;
  rows: number;
  blob_url: string;
}
export interface StopForm {
  environment: EnvironmentTarget;
  namespace: string;
  workflow_id: string;
  confirm_production: boolean;
  confirmation_text: string;
}
