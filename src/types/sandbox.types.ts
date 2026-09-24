export interface SandboxEvent {
  id: string;
  created_at: string;
  label: string;
  command: string;
}
export interface FullRlblTestForm {
  machine_ids: string;
  fst_namespace: string;
  lst_namespace: string;
  pipeline_name: string;
  manifest_path: string;
  runtime_patch: boolean;
  persist_dev_lst: boolean;
  seed_dev_lst: boolean;
  feature_fetch_mode: string;
  test_mode: string;
  wide_range_since: string;
  wide_range_until: string;
  memory_mb: number;
}
