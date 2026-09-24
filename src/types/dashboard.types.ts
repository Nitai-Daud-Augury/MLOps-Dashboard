import type { MachineStatus } from "./machine.types";

export interface DashboardSummary {
  machine_count: number;
  fully_backfilled: number;
  needs_backfill: number;
  unknown: number;
  missing_partitions: number;
  partitions_with_missing_features: number;
  partitions_with_scan_errors: number;
  production_rows: number;
  silver_rows?: number;
  coverage_start_month?: string;
  eligible_partitions: number;
  completed_eligible_partitions: number;
  no_source_partitions: number;
}
export interface DashboardSnapshot {
  scan_id: string;
  generated_at: string;
  source_account: string;
  source_container: string;
  target_features: string[];
  summary: DashboardSummary;
  machines: MachineStatus[];
  warnings: string[];
}
export interface ScanState {
  scan_id: string;
  running: boolean;
  started_at?: string;
  last_progress_at?: string;
  last_heartbeat_at?: string;
  finished_at?: string;
  error?: string;
  source_account?: string;
  source_container?: string;
  phase?: string;
  completed_partitions: number;
  total_partitions: number;
  completed_machines: number;
  total_machines: number;
}
export interface StatusPayload {
  scan_state: ScanState;
  snapshot: DashboardSnapshot;
}
export type Tab = "monitor" | "admin" | "sandbox";
