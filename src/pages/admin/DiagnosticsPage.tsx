import { Activity } from 'lucide-react';
import { useState } from 'react';
import { Banner, EmptyState, ErrorState, Spinner, StatusBadge, Timestamp } from '../../components/ui';
import { type OuterboundsProbeStatus, type ProbeResult, type ProbeStageStatus } from '../../components/outerbounds-probe/probeApi';
import { DataTable, type Column } from '../../components/ui/DataTable';
import { PageHeader, Section } from '../../components/ui/Layout';
import { ApiError, errorText, postJson, useResource } from '../../lib/api';
import type { Tone } from '../../lib/status';

const STAGE_TONE: Record<ProbeStageStatus, Tone> = { pass: 'success', fail: 'failed', skip: 'na', expected_fail: 'warn' };
const STAGE_LABEL: Record<ProbeStageStatus, string> = { pass: 'pass', fail: 'fail', skip: 'skip', expected_fail: 'expected fail' };

type Stage = ProbeResult['stages'][number];
const STAGE_COLUMNS: Column<Stage>[] = [
  { key: 'stage', header: 'Stage', cell: (st) => <strong>{st.name}</strong> },
  { key: 'result', header: 'Result', cell: (st) => <StatusBadge tone={STAGE_TONE[st.status]} label={`${STAGE_LABEL[st.status]}${st.http_status ? ` · ${st.http_status}` : ''}`} /> },
  { key: 'detail', header: 'Detail', cell: (st) => <div className="cell-stack wrap"><span>{st.detail}</span>{st.hint ? <span className="muted">Hint: {st.hint}</span> : null}</div> },
  { key: 'time', header: 'Time', optional: true, cell: (st) => <span className="mono muted">{st.ms} ms{st.error_type ? ` · ${st.error_type}` : ''}</span>, sort: (a, b) => a.ms - b.ms },
];

/** Outerbounds connectivity probe (backend OUTERBOUNDS_PROBE_ENABLED; 404 = disabled). */
export function DiagnosticsPage() {
  const status = useResource<OuterboundsProbeStatus>('/api/diagnostics/outerbounds/status', { cacheKey: 'probe-status', maxAgeMs: 0 });
  const [flow, setFlow] = useState('');
  const [includeTrigger, setIncludeTrigger] = useState(false);
  const [includeGha, setIncludeGha] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ProbeResult | null>(null);
  const probe = status.data;
  const disabled = !probe && status.error !== null && /404|not found/i.test(status.error);
  const shown = result ?? probe?.last_result ?? null;
  const selectedFlow = flow || probe?.flow_allowlist[0] || '';

  const run = async () => {
    setRunning(true); setError(null);
    try {
      const body: Record<string, unknown> = { flow: selectedFlow };
      if (probe?.trigger_enabled && includeTrigger) body.include_trigger = true;
      if (probe?.github_dispatch?.dispatch_enabled && includeGha) body.include_gha_dispatch = true;
      setResult(await postJson<ProbeResult>('/api/diagnostics/outerbounds', body));
    } catch (exc) { setError(exc instanceof ApiError ? exc.message : errorText(exc)); } finally { setRunning(false); }
  };

  if (status.loading) return <div className="card section"><Spinner /> Loading probe status…</div>;
  if (disabled || (probe && !probe.enabled)) return <EmptyState title="Probe disabled">Set OUTERBOUNDS_PROBE_ENABLED=1 on the backend to enable the Outerbounds connectivity probe.</EmptyState>;
  if (!probe) return <ErrorState message={status.error ?? 'Probe status unavailable'} onRetry={() => void status.reload()} />;
  const gha = probe.github_dispatch;
  return <>
    <PageHeader title="Diagnostics" purpose="Can the dashboard reach Outerbounds and GitHub Actions? Read-only unless you enable the trigger stage."
      meta={<><span>Read-only checks against <span className="mono">{probe.domain}</span> (perimeter {probe.perimeter}). Credentials: {probe.credentials_source}. Dispatch backend: <span className="mono">{probe.dispatch_backend ?? 'none'}</span>.</span>
      {gha ? <span className="muted">GitHub Actions: <span className="mono">{gha.workflow}@{gha.ref}</span> in <span className="mono">{gha.repo}</span>, token {gha.token_configured ? 'configured' : 'not configured'}.</span> : null}</>} />
    {probe.python && !probe.python.ok ? <Banner tone="error" title="Child Python unavailable">{probe.python.error}</Banner> : null}
    <Section title="Connectivity probe" description="Each stage runs in order and reports pass or fail with a hint.">
      <div className="form-row">
        <label className="field">Flow<select value={selectedFlow} onChange={(e) => setFlow(e.target.value)} disabled={running}>
          {probe.flow_allowlist.map((name) => <option key={name}>{name}</option>)}</select></label>
        {gha?.dispatch_enabled ? <label className="check" title="Dispatches outerbounds-dispatch with action=list_runs (read-only on Outerbounds). Takes minutes."><input type="checkbox" checked={includeGha} onChange={(e) => setIncludeGha(e.target.checked)} disabled={running} />Include GitHub Actions dispatch (list_runs)</label> : null}
        {probe.trigger_enabled ? <label className="check"><input type="checkbox" checked={includeTrigger} onChange={(e) => setIncludeTrigger(e.target.checked)} disabled={running} />Include trigger stage</label> : null}
        <button type="button" className="btn btn-primary" onClick={() => void run()} disabled={running || !selectedFlow}>{running ? <Spinner /> : <Activity size={14} />}{running ? 'Running probe…' : 'Run probe'}</button>
      </div>
      {error ? <Banner tone="error" title="Probe request failed">{error}</Banner> : null}
      {running ? <p className="muted" role="status"><Spinner /> Running stages (each capped at {probe.stage_timeout_seconds}s)…</p> : null}
      {shown ? <>
        <div className="toolbar"><StatusBadge tone={STAGE_TONE[shown.overall]} label={`overall ${STAGE_LABEL[shown.overall]}`} />
          <span className="muted">last run <Timestamp value={shown.finished_at} /> · {shown.ms} ms · {shown.flow}</span>
          {shown.counts ? <span className="mono muted">pass {shown.counts.pass} · fail {shown.counts.fail} · expected_fail {shown.counts.expected_fail} · skip {shown.counts.skip}</span> : null}</div>
        <div className="table-wrap"><DataTable label="Probe stages" prefix="probe" defaultSize={25} rows={shown.stages} columns={STAGE_COLUMNS} rowKey={(st) => st.id}
          rowClassName={(st) => (st.status === 'fail' ? 'failed' : undefined)} /></div>
      </> : <EmptyState title="No probe has run yet">Nothing is triggered unless you enable the trigger stage.</EmptyState>}
    </Section>
  </>;
}
