export interface RunningWorkflow {
  workflow_id: string;
  flow_name: string;
  namespace: string;
  status: string;
  branch?: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  outerbounds_url: string;
  machine_ids?: string[];
  /** Dashboard dispatch the status source has not reported yet (GitHub Actions / Metaflow backends). */
  pending?: boolean;
  pending_detail?: string;
  github_run_url?: string | null;
}
export interface RunningWorkflowsPayload {
  workflows: RunningWorkflow[];
  command: string[];
  error?: string;
  argo_available?: boolean;
  hint?: string;
  refreshing?: boolean;
  fetched_at?: string | null;
  pending_count?: number;
  snapshot_version?: number;
  refresh?: WorkflowRefreshInfo;
}
export interface WorkflowRefreshInfo {
  /** started | joined | limited | deferred | unavailable (absent on pushed updates). */
  state?: string;
  retry_after?: number | null;
  refreshing?: boolean;
  cooldown_seconds?: number;
}
export interface WorkflowSummaryPayload extends RunningWorkflowsPayload {
  counts: { active?: number; completed?: number; failed?: number };
  reviewed_workflow_ids?: string[];
}
