import { Send, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { DEFAULT_DEV_NAMESPACE, PROD_CONFIRMATION } from '../../constants';
import { Banner, EmptyState, ErrorState, SkeletonRows, Spinner, StatusBadge, Timestamp } from '../../components/ui';
import { useToast } from '../../components/ui/toastContext';
import { MachineStatusBadge, MachineTitle } from '../../features/machines/MachineSummary';
import { cancelBeforeSubmit, canCancelBeforeSubmit, phaseLabel } from '../../features/backfill/actions';
import { startAdminActionPoll } from '../../features/backfill/adminActionPoll';
import { buildOrchestratedPayload } from '../../features/backfill/buildOrchestratedPayload';
import { buildMonthPlan, type MonthMode } from '../../features/backfill/monthPlan';
import { isTriggerPreparationActive } from '../../features/backfill/submissionHelpers';
import type { SplitSpec } from '../../features/backfill/types';
import { inclusiveEndDateToExclusiveUntil, previewWindows, startDateToSince } from '../../features/backfill/windows';
import { errorText, postJson, useResource } from '../../lib/api';
import { plural } from '../../lib/format';
import { useStatus } from '../../state/statusStore';
import type { AdminAction, AdminReadiness, AdminSpec, BackfillScope, BusyMachineInfo, EnvironmentTarget, ManifestResult, TriggerParams } from '../../types';
import { orchestratorMonthIndex } from '../../utils';

const STEPS = ['Scope', 'Range / split', 'Estimate review', 'Submit', 'Track'];
const TRACK = [
  { phase: 'cancel_window', label: 'Queued' }, { phase: 'validating', label: 'Validating' }, { phase: 'preparing_manifests', label: 'Manifests' },
  { phase: 'submitting', label: 'Dispatching' }, { phase: 'accepted', label: 'Submitted' },
];

function RunStepper({ action }: { action: AdminAction }) {
  const phase = action.phase === 'queued' ? 'cancel_window' : action.phase ?? '';
  const failed = phase === 'failed' || action.status === 'failed';
  const cancelled = phase === 'cancelled';
  const index = TRACK.findIndex((step) => step.phase === phase);
  return <ol className="stepper" aria-live="polite">
    {TRACK.map((step, i) => <li key={step.phase} className={i < index || phase === 'accepted' ? 'done' : i === index ? 'current' : ''}>
      {i < index || phase === 'accepted' ? '✓' : i === index ? <Spinner /> : '○'} {step.label}</li>)}
    {failed ? <li className="failed">✗ Failed</li> : cancelled ? <li className="failed">— Cancelled</li> : phase === 'accepted' ? <li className="current">⧗ Running → track in Live Ops</li> : null}
  </ol>;
}

export function BackfillPage() {
  const notify = useToast();
  const { payload, loading: statusLoading, error: statusError, reload: reloadStatus } = useStatus();
  const spec = useResource<AdminSpec>('/api/admin/spec', { cacheKey: 'admin-spec', maxAgeMs: 30 * 60_000 });
  const readiness = useResource<AdminReadiness>('/api/admin/readiness', { cacheKey: 'admin-readiness', maxAgeMs: 0 });
  const locks = useResource<{ busy_machines: Record<string, BusyMachineInfo> }>('/api/backfill/runner-lock-status', { cacheKey: 'runner-busy-status', maxAgeMs: 0 });
  const actions = useResource<{ actions: AdminAction[] }>('/api/admin/actions', { cacheKey: 'admin-actions', maxAgeMs: 0 });

  const [scope, setScope] = useState<BackfillScope>('one');
  const [selected, setSelected] = useState<string[]>([]);
  const [search, setSearch] = useState('');
  const [monthMode, setMonthMode] = useState<MonthMode>('gaps');
  const [startDay, setStartDay] = useState('');
  const [endDay, setEndDay] = useState('');
  const [split, setSplit] = useState<SplitSpec>({ mode: 'month' });
  const [concurrency, setConcurrency] = useState(1);
  const [environment, setEnvironment] = useState<EnvironmentTarget>('dev');
  const [prodConfirmed, setProdConfirmed] = useState(false);
  const [namespace, setNamespace] = useState('');
  const [reviewed, setReviewed] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [action, setAction] = useState<AdminAction | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [manifest, setManifest] = useState<ManifestResult | null>(null);
  const inFlight = useRef(false);

  const machines = useMemo(() => payload?.snapshot.machines ?? [], [payload]);
  const busy = locks.data?.busy_machines;
  const lockLoading = !busy && !locks.error;
  const isLocked = (id: string) => Boolean(busy && id in busy);
  const targetIds = scope === 'all' ? machines.map((m) => m.machine_id) : selected;
  const since = monthMode === 'range' && startDay ? startDateToSince(startDay) : '';
  const until = monthMode === 'range' && endDay ? inclusiveEndDateToExclusiveUntil(endDay) : '';
  const plan = useMemo(() => buildMonthPlan(machines, targetIds, monthMode, since, until), [machines, targetIds, monthMode, since, until]);
  const planned = Object.keys(plan);
  const partitions = Object.values(plan).reduce((sum, list) => sum + list.length, 0);
  const windows = useMemo(() => {
    let count = 0;
    for (const [id, indices] of Object.entries(plan)) {
      const machine = machines.find((m) => m.machine_id === id);
      for (const month of machine?.months ?? []) {
        if (!indices.includes(orchestratorMonthIndex(month.partition))) continue;
        const { year, month: mm } = month.partition;
        const s = new Date(Date.UTC(year, mm - 1, 1)).toISOString();
        const u = new Date(Date.UTC(year, mm, 1)).toISOString();
        count += previewWindows(s, u, split).length;
      }
    }
    return count;
  }, [plan, machines, split]);
  const maxConcurrency = Math.max(1, planned.length);
  const effectiveConcurrency = scope === 'one' ? 1 : Math.min(maxConcurrency, Math.max(1, concurrency));
  const ns = namespace || spec.data?.default_dev_namespace || DEFAULT_DEV_NAMESPACE;
  const prodText = spec.data?.prod_confirmation ?? PROD_CONFIRMATION;

  // Any edit invalidates the estimate review.
  useEffect(() => { setReviewed(false); }, [scope, selected, monthMode, startDay, endDay, split, concurrency, environment, namespace]);

  // Resume a trigger that is still being prepared (e.g. after a reload).
  useEffect(() => {
    if (action) return;
    const open = actions.data?.actions.find((a) => a.action === 'trigger' && isTriggerPreparationActive(a));
    if (open) setAction(open);
  }, [actions.data, action]);
  const pollId = isTriggerPreparationActive(action) ? action?.id ?? null : null;
  useEffect(() => {
    if (!pollId) return;
    return startAdminActionPoll(pollId, {
      onUpdate: setAction,
      onAccepted: () => { notify('success', 'Backfill submitted. It shows as a pending row in Live Ops until the status snapshot reports it.'); },
      onFailed: (failed) => { notify('error', failed.error ?? 'Backfill submission failed.'); },
      onTerminal: () => { void locks.reload(); void actions.reload(); },
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollId]);

  const preparing = isTriggerPreparationActive(action);
  const readinessData = readiness.data;
  const spNotConfigured = readinessData?.argo.error_type === 'outerbounds_sp_not_configured';
  const blockers = [
    ...(preparing || submitting ? ['A backfill submission is already in progress.'] : []),
    ...(!readinessData ? ['Readiness checks are still loading.'] : []),
    ...(readinessData && !readinessData.manifest_storage.ready ? [readinessData.manifest_storage.detail ?? 'Manifest storage is not ready.'] : []),
    ...(readinessData && !readinessData.argo.ready && !spNotConfigured ? [readinessData.argo.detail ?? 'Workflow backend is not ready.'] : []),
    ...(lockLoading ? ['Machine backfill locks are still loading.'] : []),
    ...(targetIds.length === 0 ? ['Select at least one machine.'] : []),
    ...(partitions === 0 && targetIds.length ? ['No online, backfillable months in this selection.'] : []),
    ...(monthMode === 'range' && (!since || !until) ? ['Choose a start and end day.'] : []),
    ...(targetIds.some(isLocked) ? ['A selected machine already has an active backfill.'] : []),
    ...(environment === 'prod' && !prodConfirmed ? ['Confirm the production run.'] : []),
  ];

  const params = (): TriggerParams => ({
    environment, namespace: ns, storage_account_manifest_path: '', include_features_to_backfill: false, features_to_backfill: [],
    max_parallel_steps: effectiveConcurrency, force_sessions_from_bucket: false,
    confirm_production: environment === 'prod' && prodConfirmed, confirmation_text: environment === 'prod' && prodConfirmed ? prodText : '',
  });
  const body = (withParams: boolean) => buildOrchestratedPayload({ machineIds: planned, range: { since, until }, monthIndicesByMachine: plan, split, ...(withParams ? { params: params() as unknown as Record<string, unknown> } : {}) });

  const submit = async () => {
    if (inFlight.current || blockers.length) return;
    inFlight.current = true; setSubmitting(true); setSubmitError(null);
    try {
      const result = await postJson<{ action?: AdminAction }>('/api/admin/backfills/orchestrated', body(true));
      if (!result.action?.id) throw new Error('The server accepted no backfill action record. Check Admin overview before retrying.');
      setAction(result.action);
      notify('info', `Backfill queued (${result.action.id.slice(0, 8)}); you can cancel it before dispatch.`);
    } catch (exc) { setSubmitError(errorText(exc)); notify('error', `Backfill failed: ${errorText(exc)}`); }
    finally { inFlight.current = false; setSubmitting(false); }
  };
  const manifestsOnly = async () => {
    setSubmitting(true); setSubmitError(null);
    try {
      const result = await postJson<{ manifest?: ManifestResult }>('/api/admin/manifests/orchestrated', body(false));
      setManifest(result.manifest ?? null);
      notify('success', 'Manifests created; no backfill was started.');
    } catch (exc) { setSubmitError(errorText(exc)); } finally { setSubmitting(false); }
  };
  const cancel = async () => {
    if (!action) return;
    setCancelling(true);
    try { setAction(await cancelBeforeSubmit(action.id)); notify('success', 'Backfill cancelled before dispatch.'); }
    catch (exc) { notify('error', errorText(exc)); } finally { setCancelling(false); }
  };

  const step = action ? 5 : reviewed ? 4 : targetIds.length && partitions ? 3 : targetIds.length ? 2 : 1;
  const visible = machines.filter((m) => m.machine_id.toLowerCase().includes(search.trim().toLowerCase()) || (m.display_name ?? '').toLowerCase().includes(search.trim().toLowerCase())).slice(0, 200);
  const toggle = (id: string) => setSelected((current) => scope === 'one' ? [id] : current.includes(id) ? current.filter((x) => x !== id) : [...current, id]);

  if (!payload && statusError) return <ErrorState message={statusError} onRetry={() => void reloadStatus()} />;
  return <>
    <div className="page-head"><div><h1>Backfill</h1><span className="muted">Same-machine months always run sequentially; the backend re-checks locks and duplicates.</span></div></div>
    <Banner tone="warn" title={`ADMIN: this dispatches real workflows (env: ${environment} · ns: ${environment === 'prod' ? 'production' : ns}).`} />
    <ol className="stepper" aria-label="Progress">{STEPS.map((label, i) => <li key={label} className={i + 1 < step ? 'done' : i + 1 === step ? 'current' : ''}>{i + 1 < step ? '✓' : `${i + 1}`} {label}</li>)}</ol>

    {action ? <section className="panel">
      <div className="panel-head"><h2>Track</h2><span className="mono muted">{action.id.slice(0, 12)} · <Timestamp value={action.created_at} /></span></div>
      <RunStepper action={action} />
      <p>{phaseLabel(action.phase)}{action.machine_ids?.length ? ` · ${plural(action.machine_ids.length, 'machine')}` : ''}{action.workflow_id ? <> · <span className="mono">{action.workflow_id}</span></> : null}</p>
      {action.error ? <Banner tone="error" title="Submission failed">{action.error}</Banner> : null}
      <div className="toolbar">
        {canCancelBeforeSubmit(action) ? <button type="button" className="btn btn-danger-outline" onClick={() => void cancel()} disabled={cancelling}>{cancelling ? <Spinner /> : <X size={14} />}Cancel before dispatch</button> : null}
        {action.outerbounds_url ? <a className="btn" href={action.outerbounds_url} target="_blank" rel="noreferrer">Open in Outerbounds</a> : null}
        <Link className="btn" to="/monitor/live">Open Live Ops</Link>
        {!preparing ? <button type="button" className="btn" onClick={() => { setAction(null); setReviewed(false); }}>New backfill</button> : null}
      </div>
    </section> : null}

    <section className="panel" aria-labelledby="scope-h">
      <div className="panel-head"><h2 id="scope-h">① Scope</h2>
        <div className="fs-control" role="radiogroup" aria-label="Scope">{(['one', 'selected', 'all'] as BackfillScope[]).map((value) =>
          <button key={value} type="button" role="radio" aria-checked={scope === value} className={`seg${scope === value ? ' seg-on' : ''}`}
            onClick={() => { setScope(value); setSelected((s) => value === 'one' ? s.slice(0, 1) : s); }}>{value === 'one' ? 'One machine' : value === 'selected' ? 'Selected' : 'All machines'}</button>)}</div>
      </div>
      {scope !== 'all' ? <div className="toolbar"><input type="search" placeholder="Search machines…" aria-label="Search machines" value={search} onChange={(e) => setSearch(e.target.value)} />
        <span className="muted">{plural(selected.length, 'machine')} selected</span>{selected.length ? <button type="button" className="btn" onClick={() => setSelected([])}>Clear</button> : null}</div>
        : <p>All {plural(machines.length, 'machine')} in the last scan{machines.some((m) => isLocked(m.machine_id)) ? <span className="field-error"> · some are busy (blocked)</span> : null}.</p>}
      {scope !== 'all' ? <div className="table-wrap" style={{ maxHeight: 360 }}><table>
        <thead><tr><th scope="col"><span className="sr-only">Select</span></th><th scope="col">Machine</th><th scope="col">Status</th><th scope="col">Coverage</th><th scope="col">Backfill</th></tr></thead>
        <tbody>{statusLoading && !machines.length ? <tr><td colSpan={5}><Spinner /> Loading machine inventory…</td></tr> : visible.map((m) => {
          const locked = isLocked(m.machine_id);
          return <tr key={m.machine_id}>
            <td><input type={scope === 'one' ? 'radio' : 'checkbox'} name="machine" aria-label={`Select ${m.machine_id}`} checked={selected.includes(m.machine_id)} disabled={locked || lockLoading} onChange={() => toggle(m.machine_id)} /></td>
            <td><MachineTitle machine={m} /></td><td><MachineStatusBadge machine={m} /></td><td className="mono">{m.months_complete}/{m.months_expected}</td>
            <td>{lockLoading ? <span className="muted"><Spinner /> Checking…</span> : locked ? <StatusBadge tone="running" label="Busy" title={busy?.[m.machine_id]?.workflow_id} /> : <StatusBadge tone="success" label="Ready" />}</td>
          </tr>;
        })}{!statusLoading && !machines.length ? <tr><td colSpan={5}><EmptyState title="No machines">Run a scan in Monitoring first.</EmptyState></td></tr> : null}
          {statusLoading && machines.length === 0 ? <SkeletonRows columns={5} rows={2} /> : null}</tbody></table></div> : null}
      {locks.error ? <p className="field-error">Lock status unavailable: {locks.error}</p> : null}
    </section>

    <section className="panel" aria-labelledby="range-h">
      <div className="panel-head"><h2 id="range-h">② Range / split</h2></div>
      <div className="form-row">
        <label className="field">Months<select value={monthMode} onChange={(e) => setMonthMode(e.target.value as MonthMode)}>
          <option value="gaps">Gaps only (needs backfill)</option><option value="all">All backfillable months</option><option value="range">Date range</option></select></label>
        {monthMode === 'range' ? <>
          <label className="field">Start day<input type="date" value={startDay} onChange={(e) => setStartDay(e.target.value)} /></label>
          <label className="field">End day (inclusive)<input type="date" value={endDay} onChange={(e) => setEndDay(e.target.value)} /></label></> : null}
        <label className="field">Split<select value={split.mode} onChange={(e) => setSplit({ mode: e.target.value as SplitSpec['mode'], days: split.days ?? 3 })}>
          <option value="month">Monthly</option><option value="week">Weekly</option><option value="day">Daily</option><option value="days">Every N days</option></select></label>
        {split.mode === 'days' ? <label className="field">N days<input type="number" min={1} max={31} value={split.days ?? 3} onChange={(e) => setSplit({ mode: 'days', days: Number(e.target.value) })} /></label> : null}
        {scope !== 'one' ? <label className="field">Machines in parallel<input type="number" min={1} max={maxConcurrency} value={effectiveConcurrency} onChange={(e) => setConcurrency(Number(e.target.value))} /></label> : null}
        <label className="field">Environment<select value={environment} onChange={(e) => { setEnvironment(e.target.value as EnvironmentTarget); setProdConfirmed(false); }}>
          <option value="dev">dev</option><option value="prod">prod</option></select></label>
        {environment === 'dev' ? <label className="field">Namespace<input value={namespace} placeholder={ns} onChange={(e) => setNamespace(e.target.value)} /></label>
          : <label className="check"><input type="checkbox" checked={prodConfirmed} onChange={(e) => setProdConfirmed(e.target.checked)} />I confirm a PRODUCTION backfill</label>}
      </div>
      {split.mode !== 'month' ? <p className="muted">Day/week/N-day splits need the orchestrator contract v5.</p> : null}
    </section>

    <section className="panel" aria-labelledby="review-h">
      <div className="panel-head"><h2 id="review-h">③ Estimate review</h2>
        <button type="button" className="btn" disabled={!targetIds.length} onClick={() => setReviewed(true)}>{reviewed ? 'Reviewed ✓' : 'Review estimate'}</button></div>
      <p><strong>{plural(planned.length, 'machine')}</strong> · <strong>{plural(partitions, 'month')}</strong> · <strong>{plural(windows, 'manifest window')}</strong> · {effectiveConcurrency} machine(s) at a time, months sequential per machine.</p>
      {reviewed && planned.length ? <details open={planned.length <= 10}><summary>Per-machine months</summary><ul>{Object.entries(plan).map(([id, indices]) => {
        const machine = machines.find((m) => m.machine_id === id);
        const labels = machine?.months.filter((m) => indices.includes(orchestratorMonthIndex(m.partition))).map((m) => m.partition.label) ?? [];
        return <li key={id}><span className="mono">{id}</span>: {labels.join(', ')}</li>;
      })}</ul></details> : null}
      {blockers.length ? <ul className="muted">{blockers.map((b) => <li key={b} className="wrap">{b}</li>)}</ul> : null}
    </section>

    <section className="panel" aria-labelledby="submit-h">
      <div className="panel-head"><h2 id="submit-h">④ Submit</h2></div>
      {submitError ? <Banner tone="error" title="Request failed" onDismiss={() => setSubmitError(null)}>{submitError}</Banner> : null}
      {manifest ? <Banner tone="info" title="Manifests created" onDismiss={() => setManifest(null)}><span className="mono">{manifest.manifest_prefix ?? manifest.manifest_path}</span> ({manifest.manifest_count ?? manifest.rows} files)</Banner> : null}
      <div className="toolbar">
        <button type="button" className="btn btn-primary" onClick={() => void submit()} disabled={!reviewed || blockers.length > 0 || submitting}>
          {submitting ? <Spinner /> : <Send size={14} />}{submitting ? 'Submitting…' : 'Submit backfill'}</button>
        <button type="button" className="btn" onClick={() => void manifestsOnly()} disabled={!reviewed || submitting || planned.length === 0}>Create manifests only</button>
        {!reviewed ? <span className="muted">Review the estimate first.</span> : null}
      </div>
    </section>
  </>;
}
