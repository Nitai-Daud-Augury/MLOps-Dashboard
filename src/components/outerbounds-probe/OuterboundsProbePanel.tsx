import { Activity, ChevronDown, ChevronRight, Loader2 } from 'lucide-react';
import { useCallback, useState } from 'react';
import { PROBE_URL, type OuterboundsProbeStatus, type ProbeResult, type ProbeStage, type ProbeStageStatus } from './probeApi';

const STATUS_LABEL: Record<ProbeStageStatus, string> = { pass: 'Pass', fail: 'Fail', expected_fail: 'Expected fail', skip: 'Skipped' };
/** Long details start collapsed (CSS line clamp) but the full text is always in the DOM. */
export const DETAIL_COLLAPSE_CHARS = 280;

export function ProbeStatusBadge({ status }: { status: ProbeStageStatus }) {
  return <span className={`probe-badge ${status}`} data-status={status}>{STATUS_LABEL[status] ?? status}</span>;
}

/** Dotted exception names (metaflow.exception.MetaflowNotFound) may wrap after each dot. */
function DottedName({ value }: { value: string }) {
  const parts = value.split('.');
  return <>{parts.map((part, index) => <span key={index}>{part}{index < parts.length - 1 ? <>.<wbr /></> : null}</span>)}</>;
}

function StageDetail({ stage }: { stage: ProbeStage }) {
  const long = stage.detail.length > DETAIL_COLLAPSE_CHARS;
  const [expanded, setExpanded] = useState(false);
  return <div className="probe-detail-cell">
    <div className={`probe-detail-text${long && !expanded ? ' clamped' : ''}`} data-testid={`probe-detail-${stage.id}`}>{stage.detail || '—'}</div>
    {long ? <button className="probe-detail-toggle" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
      {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}{expanded ? 'Show less' : `Show full detail (${stage.detail.length} chars)`}
    </button> : null}
    {stage.hint ? <p className={`probe-hint ${stage.status}`} data-testid={`probe-hint-${stage.id}`}><strong>What this means: </strong>{stage.hint}</p> : null}
  </div>;
}

function StageRow({ stage }: { stage: ProbeStage }) {
  return <tr className={`probe-row ${stage.status}`} data-stage={stage.id}>
    <td className="probe-stage-cell" data-label="Stage">
      <span className="probe-stage-name">{stage.name}</span>
      {stage.description ? <span className="probe-stage-description">{stage.description}</span> : null}
    </td>
    <td className="probe-status-cell" data-label="Status">
      <ProbeStatusBadge status={stage.status} />
      {stage.http_status != null ? <span className="probe-http">HTTP {stage.http_status}</span> : null}
    </td>
    <td className="probe-error-cell mono" data-label="Error type">{stage.error_type ? <DottedName value={stage.error_type} /> : '—'}</td>
    <td className="probe-detail-td" data-label="Detail"><StageDetail stage={stage} /></td>
    <td className="num" data-label="ms">{stage.ms}</td>
  </tr>;
}

async function readError(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const payload = JSON.parse(text) as { detail?: unknown };
    if (typeof payload.detail === 'string') return payload.detail;
    if (payload.detail != null) return JSON.stringify(payload.detail);
  } catch {
    // not JSON (e.g. an HTML error page from a proxy)
  }
  return `Probe request failed (HTTP ${response.status})${text ? `: ${text.slice(0, 300)}` : ''}`;
}

export function ProbeResultView({ result }: { result: ProbeResult }) {
  return <>
    <p className="probe-summary">
      <span>Overall</span> <ProbeStatusBadge status={result.overall} />
      <span>flow {result.flow}</span><span>{result.ms} ms</span><span>{new Date(result.finished_at).toLocaleString()}</span>
      {result.counts ? <span className="probe-counts">{result.counts.pass} pass · {result.counts.fail} fail · {result.counts.expected_fail} expected fail · {result.counts.skip} skipped</span> : null}
    </p>
    <div className="probe-table-wrap">
      <table className="probe-table">
        <colgroup><col className="probe-col-stage" /><col className="probe-col-status" /><col className="probe-col-error" /><col /><col className="probe-col-ms" /></colgroup>
        <thead><tr><th>Stage</th><th>Status</th><th>Error type</th><th>Detail</th><th className="num">ms</th></tr></thead>
        <tbody>{result.stages.map((stage) => <StageRow key={stage.id} stage={stage} />)}</tbody>
      </table>
    </div>
  </>;
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
      if (!response.ok) throw new Error(await readError(response));
      setResult(await response.json() as ProbeResult);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setRunning(false);
    }
  }, [flow, includeTrigger, status.trigger_enabled]);

  return <section className="outerbounds-probe-panel admin-view-panel diagnostics-panel">
    <header className="panel-header probe-header">
      <div className="probe-header-text">
        <p className="eyebrow">Diagnostics</p>
        <h2>Outerbounds connectivity probe</h2>
        <p>Read-only checks against <span className="mono">{status.domain}</span> (perimeter {status.perimeter}): network, Metaflow import, auth and run listing. Credentials: {status.credentials_source}.</p>
        {status.python?.ok ? <p className="probe-meta">Child Python: <span className="mono">{status.python.path}</span> ({status.python.source})</p> : null}
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
    {status.python && !status.python.ok ? <div className="probe-error" role="alert">Child Python unavailable: {status.python.error}</div> : null}
    {error ? <div className="probe-error" role="alert">{error}</div> : null}
    {result ? <ProbeResultView result={result} /> : <p className="probe-empty">No probe has run yet. Each stage is capped at {status.stage_timeout_seconds}s; nothing is triggered.</p>}
  </section>;
}
