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
}
export interface RunningWorkflowsPayload {
  workflows: RunningWorkflow[];
  command: string[];
  error?: string;
  argo_available?: boolean;
  hint?: string;
}
export interface WorkflowSummaryPayload extends RunningWorkflowsPayload {
  counts: { active?: number; completed?: number; failed?: number };
  reviewed_workflow_ids?: string[];
}
