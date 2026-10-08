import { Activity, Loader2 } from 'lucide-react';
import { useCallback, useState } from 'react';
import { PROBE_URL, type OuterboundsProbeStatus, type ProbeResult, type ProbeStageStatus } from './probeApi';

const STATUS_LABEL: Record<ProbeStageStatus, string> = { pass: 'Pass', fail: 'Fail', skip: 'Skipped', expected_fail: 'Expected fail' };

export function ProbeStatusBadge({ status }: { status: ProbeStageStatus }) {
  return <span className={`probe-badge ${status}`}>{STATUS_LABEL[status] ?? status}</span>;
}

export function OuterboundsProbePanel({ status }: { status: OuterboundsProbeStatus }) {
  const [flow, setFlow] = useState(status.flow_allowlist[0] ?? '');
  const [includeTrigger, setIncludeTrigger] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ProbeResult | null>(status.last_result);

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      const body: Record<string, unknown> = { flow };
      if (status.trigger_enabled && includeTrigger) body.include_trigger = true;
      const response = await fetch(PROBE_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const payload = await response.json() as ProbeResult | { detail?: string };
      if (!response.ok) throw new Error(('detail' in payload && payload.detail) || `Probe failed (HTTP ${response.status})`);
      setResult(payload as ProbeResult);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setRunning(false);
    }
  }, [flow, includeTrigger, status.trigger_enabled]);

  return <section className="outerbounds-probe-panel admin-view-panel diagnostics-panel">
    <header className="panel-header">
      <div>
        <p className="eyebrow">Diagnostics</p>
        <h2>Outerbounds connectivity probe</h2>
        <p>Read-only checks against {status.domain} (perimeter {status.perimeter}): network, Metaflow import, auth and run listing. Credentials: {status.credentials_source}.</p>
      </div>
      <div className="actions probe-actions">
        <label className="probe-flow">Flow
          <select value={flow} onChange={(event) => setFlow(event.target.value)} disabled={running}>
            {status.flow_allowlist.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        {status.trigger_enabled ? <label className="probe-trigger"><input type="checkbox" checked={includeTrigger} onChange={(event) => setIncludeTrigger(event.target.checked)} disabled={running} />Include trigger stage</label> : null}
        <button className="primary-button" type="button" onClick={() => void run()} disabled={running || !flow}>
          {running ? <Loader2 className="spin" size={16} /> : <Activity size={16} />}{running ? 'Running probe…' : 'Run probe'}
        </button>
      </div>
    </header>
    {error ? <div className="probe-error" role="alert">{error}</div> : null}
    {result ? <>
      <p className="probe-summary">Overall <ProbeStatusBadge status={result.overall} /> · flow {result.flow} · {result.ms} ms · {new Date(result.finished_at).toLocaleString()}</p>
      <table className="probe-table">
        <thead><tr><th>Stage</th><th>Status</th><th>Detail</th><th>Error type</th><th className="num">ms</th></tr></thead>
        <tbody>{result.stages.map((stage) => <tr key={stage.id}>
          <td>{stage.name}</td>
          <td><ProbeStatusBadge status={stage.status} /></td>
          <td className="probe-detail">{stage.detail}</td>
          <td className="mono">{stage.error_type ?? '—'}</td>
          <td className="num">{stage.ms}</td>
        </tr>)}</tbody>
      </table>
    </> : <p className="probe-empty">No probe has run yet. Each stage is capped at {status.stage_timeout_seconds}s; nothing is triggered.</p>}
  </section>;
}
