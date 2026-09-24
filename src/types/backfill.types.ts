export type BackfillStatus =
  | "backfilled"
  | "needs_backfill"
  | "no_source_data"
  | "not_backfillable"
  | "unknown"
  | "scan_error";

export type ActivityStatus = "not_installed" | "online" | "offline" | "unknown";

export interface MonthPartition {
  index: number;
  year: number;
  month: number;
  quarter: string;
  label: string;
  blob_path_suffix: string;
}

export interface MonthStatus {
  machine_id: string;
  partition: MonthPartition;
  status: BackfillStatus;
  reason: string;
  recommended_action: string;
  blob_path: string;
  blob_url?: string;
  row_count?: number;
  silver_row_count?: number;
  last_modified?: string;
  missing_features: string[];
  zero_count_features: string[];
  partial_features: string[];
  feature_coverage_gaps: Record<string, {
    missing_rows: number;
    first_missing_at?: string;
    last_missing_at?: string;
  }>;
  feature_non_null_counts: Record<string, number>;
  total_columns?: number;
  expected_total_columns?: number;
  missing_schema_columns: string[];
  error?: string;
  activity_status?: ActivityStatus;
}

export type ActiveMonthState =
  | "queued"
  | "running"
  | "cancel_requested"
  | "stopping"
  | "requeue_pending"
  | "completed"
  | "failed"
  | "cancelled";
export interface ActiveMonth {
  month_index: number;
  year: number;
  month: number;
  state: ActiveMonthState;
  parent_workflow_id?: string;
  child_workflow_id?: string;
  replacement_workflow_id?: string;
}
export interface BusyMachineInfo {
  source: "control_store" | "argo_running";
  workflow_id?: string;
  flow_name?: string;
  status?: string;
  months?: ActiveMonth[];
}
