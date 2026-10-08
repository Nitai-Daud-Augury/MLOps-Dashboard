import type { BackfillStatus, MonthStatus } from "./backfill.types";

export type MachineView =
  | "overview"
  | "features"
  | "backfill"
  | "running"
  | "history"
  | "observability";
export interface MachineStatus {
  machine_id: string;
  display_name?: string;
  is_test_machine?: boolean;
  augury_url: string;
  status: BackfillStatus;
  reason: string;
  recommended_action: string;
  months_complete: number;
  months_expected: number;
  coverage_start_month?: string;
  no_source_months: number;
  first_missing_month?: string;
  last_populated_month?: string;
  production_rows: number;
  silver_rows?: number;
  installation_at?: string;
  installation_month?: string;
  online_months?: number;
  offline_months?: number;
  pre_install_months?: number;
  months: MonthStatus[];
  crosscheck?: { status: 'match' | 'mismatch' | 'feature_store_stale'; flagged_months: number; stale_months?: number } | null;
  /** Lifecycle provider that ran for this scan (databricks | mongo), null when none ran. */
  lifecycle_source?: string | null;
  /** True when that provider returned a lifecycle record for this machine. */
  lifecycle_enriched?: boolean;
}
