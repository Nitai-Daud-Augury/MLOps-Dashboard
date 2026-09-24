export type Cohort = 'standard' | 'ulrpm' | 'unknown';
export type MachineRecord = {
  machine_id: string; display_name: string; status: string; resource_cohort: Cohort;
  backfill_eligible: boolean; classification_reason: string[]; endpoint_count: number;
  site_name?: string; organization_name?: string;
};
export type InventoryPage = {
  machines: MachineRecord[]; next_cursor: string | null; inventory_version: string; total_estimate?: number;
};
export type InventoryFilters = { search: string; cohort: string; status: string; eligible: string;
  site_id: string; organization_id: string; classification_issue: string; sort_by: string; sort_dir: string };
export type CapacityProfile = {
  memory_mib: number; pool: string; purchase: string; sku: string; region: string;
  recommended_concurrency: number; raw_slots: number; confidence: string; limiting_factors: Record<string, number>;
};
export type EstimateLane = {
  cohort: 'standard' | 'ulrpm'; machine_count: number; work_item_count: number;
  completion_hours: Array<number | null>; allocated_cost: Array<number | null>;
  node_cost: Array<number | null>; storage_cost: number | null;
  confidence: string; blocked: boolean; resource_profile: CapacityProfile;
  price_quote: { available: boolean; source: string; currency?: string; hourly_rate?: number };
};
export type Estimate = {
  estimate_id: string; estimate_signature: string; expires_at: string; production_ready: boolean;
  lanes: Record<'standard' | 'ulrpm', EstimateLane>; excluded_unknown_or_ineligible: number; disclaimer: string;
};
export type Campaign = {
  id: string; name: string; state: string; work_item_counts: Record<string, number>;
  cohort_counts: Record<string, number>; created_at: string;
};
export type ControlStatus = { dispatch_enabled: boolean; production_ready: boolean; blockers: string[];
  inventory: { status: string; source: string; inventory_version: string; last_sync_at?: string } };
