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
  crosscheck?: MonthCrosscheck | null;
}

/** Informational FEATURES_CROSSCHECK annotation (blob vs feature_store). Never changes status. */
export type CrosscheckFlag = 'row_ratio' | 'v2_presence' | 'missing_in_feature_store' | 'missing_in_blob' | 'feature_store_stale';
export interface MonthCrosscheck {
  status: 'match' | 'mismatch' | 'absent_both' | 'not_checked' | 'feature_store_stale';
  flags: CrosscheckFlag[];
  /** Differences found while the Databricks copy was stale; not judged as mismatches. */
  suppressed_flags?: CrosscheckFlag[];
  stale_reason?: string;
  blob_modified?: string | null;
  bronze_modified?: string | null;
  blob_rows?: number;
  feature_store_rows?: number;
  row_ratio?: number | null;
  v2_blob_only?: string[];
  v2_feature_store_only?: string[];
  detail?: string;
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
