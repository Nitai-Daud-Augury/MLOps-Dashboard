import { useEffect, useState } from 'react';
import { API_BASE } from '../../constants';

export type ProbeStageStatus = 'pass' | 'fail' | 'skip' | 'expected_fail';

export interface ProbeStage {
  id: string;
  name: string;
  status: ProbeStageStatus;
  detail: string;
  error_type: string | null;
  ms: number;
}

export interface ProbeResult {
  probe_id: string;
  started_at: string;
  finished_at: string;
  ms: number;
  overall: Exclude<ProbeStageStatus, 'skip'>;
  flow: string;
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

