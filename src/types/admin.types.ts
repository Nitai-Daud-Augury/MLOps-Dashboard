export type SourceType = "local" | "github";
export type EnvironmentTarget = "dev" | "prod";
export type BackfillScope = "one" | "selected" | "all";
export interface RuntimeCapabilities {
  monitor: boolean;
  workflow_mutations: boolean;
  manifest_creation: boolean;
}
export interface RuntimeInfo {
  mode: "local" | "docker" | "databricks";
  capabilities: RuntimeCapabilities;
}
export type AdminView =
  | "overview"
  | "backfill"
  | "campaigns"
  | "operations"
  | "reviews"
  | "cost"
  | "advanced"
  | "observability";
export interface AdminSpec {
  runtime?: RuntimeInfo;
  default_local_flow_path: string;
  default_dev_namespace: string;
  prod_namespace: string;
  prod_confirmation: string;
  stop_prod_confirmation: string;
  default_features_to_backfill: string[];
  templates?: {
    fst_backfill: string;
    ulrpm_orchestrator: string;
    fullrlbl_test: string;
  };
  known_no_data_note: string;
  outerbounds_url: string;
  outerbounds_running_url: string;
  outerbounds_past_url: string;
  params: Array<{ name: string; description: string }>;
}
export interface AdminAction {
  id: string;
  action: string;
  status: string;
  command: string[];
  cwd: string;
  created_at: string;
  finished_at?: string;
  stdout: string;
  stderr: string;
  error?: string;
  outerbounds_url?: string;
  machine_ids?: string[];
  workflow_id?: string;
  flow_name?: string;
  phase?: string;
}
export interface AdminReadinessCheck {
  ready: boolean;
  detail?: string;
  credential_source?: string;
  credential_kind?: string;
  account_name?: string;
  container_name?: string;
  template_name?: string;
}
export interface AdminReadiness {
  manifest_storage: AdminReadinessCheck;
  argo: AdminReadinessCheck;
  fullrlbl_test: AdminReadinessCheck;
}
export interface Toast {
  id: string;
  type: "success" | "error" | "info";
  message: string;
  timestamp: number;
  exiting?: boolean;
}
