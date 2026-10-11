import { useEffect, useState } from 'react';
import { API_BASE } from '../../constants';

export type ProbeStageStatus = 'pass' | 'fail' | 'skip' | 'expected_fail';

export interface ProbeStage {
  id: string;
  name: string;
  status: ProbeStageStatus;
  detail: string;
  /** Exception type, `HTTP<code>`, or a probe code such as `interpreter_missing`. */
  error_type: string | null;
  ms: number;
  /** What the stage checks (from the backend). */
  description?: string;
  /** HTTP status seen by the stage (auth), when there is one. */
  http_status?: number | null;
  /** What the result means and the next step (from the backend). */
  hint?: string | null;
}

export interface ProbeCounts {
  pass: number;
  fail: number;
  expected_fail: number;
  skip: number;
}

export interface ProbeResult {
  probe_id: string;
  started_at: string;
  finished_at: string;
  ms: number;
  overall: Exclude<ProbeStageStatus, 'skip'>;
  flow: string;
  counts?: ProbeCounts;
  stages: ProbeStage[];
}

export interface OuterboundsProbeStatus {
  enabled: boolean;
  trigger_enabled: boolean;
  domain: string;
  perimeter: string;
  hosts: string[];
  flow_allowlist: string[];
  deployment_id_configured: boolean;
  stage_timeout_seconds: number;
  credentials_source: 'env' | 'config_file' | 'none';
  /** Interpreter used for the Metaflow child process (absolute path; error set when unusable). */
  python?: { path: string; source: string; ok: boolean; error: string | null };
  metaflow_config?: { dir: string; config_file_present: boolean };
  config_keys?: { present: string[]; absent: string[] };
  /** Selected Outerbounds dispatch backend: argo_cli (local), github_actions or metaflow. */
  dispatch_backend?: string | null;
  /** GitHub Actions dispatch settings (never the token). */
  github_dispatch?: { token_configured: boolean; repo: string; ref: string; workflow: string; dispatch_enabled: boolean };
  last_result: ProbeResult | null;
}

export const PROBE_URL = `${API_BASE}/api/diagnostics/outerbounds`;

/** Probe status, or null when the probe is disabled (both routes 404 then). */
export function useOuterboundsProbeStatus(): OuterboundsProbeStatus | null {
  const [status, setStatus] = useState<OuterboundsProbeStatus | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetch(`${PROBE_URL}/status`)
      .then(async (response) => (response.ok ? (await response.json() as OuterboundsProbeStatus) : null))
      .catch(() => null)
      .then((value) => { if (!cancelled) setStatus(value && value.enabled ? value : null); });
    return () => { cancelled = true; };
  }, []);
  return status;
}

