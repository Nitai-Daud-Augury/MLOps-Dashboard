import { API_BASE } from '../../constants';

export type FeaturesSource = 'blob' | 'databricks';

export interface FeaturesSourcePersistence {
  location?: string | null;
  load_state?: string;
  load_error?: string | null;
  durable?: boolean | null;
  error?: string | null;
  fallback_path?: string | null;
}

/** GET/PUT /api/settings/features-source and data_sources.features_source in /api/backfill/status. */
export interface FeaturesSourceStatus {
  source: FeaturesSource;
  label: string;
  options: FeaturesSource[];
  default: FeaturesSource;
  origin: 'env_default' | 'persisted' | 'switched';
  updated_at?: string | null;
  updated_by?: string | null;
  previous?: FeaturesSource | null;
  persistence: FeaturesSourcePersistence;
  config_error?: string | null;
  active_source: FeaturesSource;
  results_source: FeaturesSource | null;
  results_source_label?: string | null;
  results_scanned_at?: string | null;
  results_scan_id?: string | null;
  results_source_inferred?: boolean;
  mismatch: boolean;
  rescan_required: boolean;
  scan_running: boolean;
  notes: string[];
}

export const SOURCE_NAMES: Record<FeaturesSource, string> = { blob: 'Blob', databricks: 'Databricks' };

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json() as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
    return JSON.stringify(body.detail ?? body);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

export async function putFeaturesSource(source: FeaturesSource): Promise<FeaturesSourceStatus> {
  const response = await fetch(`${API_BASE}/api/settings/features-source`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source }),
  });
  if (!response.ok) throw new Error(await readError(response));
  return await response.json() as FeaturesSourceStatus;
}
