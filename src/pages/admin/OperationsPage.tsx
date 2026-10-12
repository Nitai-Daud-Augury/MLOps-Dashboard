import { Square } from 'lucide-react';
import { useState } from 'react';
import { DEFAULT_DEV_NAMESPACE } from '../../constants';
import { Banner, ConfirmDialog, EmptyState, ErrorState, KpiChip, RefreshButton, Spinner, StatusBadge, Timestamp } from '../../components/ui';
import { useToast } from '../../components/ui/toastContext';
import { useWorkflowSummary } from '../../features/workflows/useWorkflowSummary';
import { workflowSort } from '../../features/workflows/sort';
import { EmptyWorkflows, WorkflowTable } from '../../features/workflows/WorkflowTable';
import { apiJson, errorText, postJson, useResource } from '../../lib/api';
import { monthLabel } from '../../lib/format';
import { ACTIVE_MONTH_STATES, isFinishedWorkflow, workflowTone } from '../../lib/status';
import type { ActiveMonth, AdminSpec, BusyMachineInfo, RunningWorkflow } from '../../types';

type Pending = { kind: 'terminate'; workflowId: string } | { kind: 'cancel'; machineId: string; month: ActiveMonth };

/** Running workflows with the rate-limited Refresh, stop (type the ID), cancel one month, failure review. */
export function OperationsPage() {
  const notify = useToast();
  const summary = useWorkflowSummary({ onLimited: (s) => notify('info', `Status refresh is rate limited: retry in ${s}s.`) });
  const spec = useResource<AdminSpec>('/api/admin/spec', { cacheKey: 'admin-spec', maxAgeMs: 30 * 60_000 });
  const locks = useResource<{ busy_machines: Record<string, BusyMachineInfo> }>('/api/backfill/runner-lock-status', { cacheKey: 'runner-busy-status', maxAgeMs: 60_000 });
  const [machineId, setMachineId] = useState('');
  const months = useResource<{ months: ActiveMonth[] }>(machineId ? `/api/machines/${encodeURIComponent(machineId)}/backfills/active` : null, { cacheKey: `active:${machineId}`, maxAgeMs: 0 });
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reviewBusy, setReviewBusy] = useState<string | null>(null);
  const data = summary.data;
  const all = (data?.workflows ?? []).slice().sort(workflowSort);
  const active = all.filter((w) => w.pending || !isFinishedWorkflow(w.status));
  const reviewed = new Set(data?.reviewed_workflow_ids ?? []);
  const failed = all.filter((w) => ['failed', 'error'].includes(w.status.toLowerCase()));
  const activeMonths = (months.data?.months ?? []).filter((m) => ACTIVE_MONTH_STATES.includes(m.state));
  const parent = activeMonths.find((m) => m.parent_workflow_id)?.parent_workflow_id;
  const namespace = spec.data?.default_dev_namespace ?? DEFAULT_DEV_NAMESPACE;

  const run = async () => {
    if (!pending) return;
    setBusy(true); setActionError(null);
    try {
      if (pending.kind === 'terminate') {
        await postJson('/api/admin/workflows/terminate', { params: { environment: 'dev', namespace, workflow_id: pending.workflowId, confirm_production: false, confirmation_text: '' } });
        notify('success', `Stop submitted for ${pending.workflowId}.`);
      } else {
        const m = pending.month;
        await postJson(`/api/machines/${encodeURIComponent(pending.machineId)}/backfills/${m.month_index}/cancel`, { parent_workflow_id: m.parent_workflow_id ?? '', child_workflow_id: m.child_workflow_id ?? '', requeue_month_indices: [], requested_by: 'dashboard' });
        notify('success', `Month ${monthLabel(m.year, m.month)} stop accepted; the parent continues with the next month.`);
      }
      setPending(null);
      await Promise.all([months.reload(), locks.reload()]);
    } catch (exc) { setActionError(errorText(exc)); notify('error', errorText(exc)); } finally { setBusy(false); }
  };

  const toggleReview = async (workflow: RunningWorkflow, next: boolean) => {
    setReviewBusy(workflow.workflow_id);
    try {
      const result = await apiJson<{ reviewed_workflow_ids?: string[] }>(`/api/admin/workflows/${encodeURIComponent(workflow.workflow_id)}/review`, { method: next ? 'POST' : 'DELETE' });
      if (data) summary.set({ ...data, reviewed_workflow_ids: result.reviewed_workflow_ids ?? data.reviewed_workflow_ids });
    } catch (exc) { notify('error', `Review update failed: ${errorText(exc)}`); } finally { setReviewBusy(null); }
  };

  if (!data && summary.error) return <ErrorState message={summary.error} onRetry={() => void summary.reload()} />;
  return <>
    <div className="page-head"><div><h1>Operations</h1>
      <span className="muted">{data?.fetched_at ? <Timestamp value={data.fetched_at} prefix="Last fetched" /> : 'Status not fetched yet'} · snapshot v{data?.snapshot_version ?? '—'}</span></div>
      <RefreshButton onClick={() => void summary.refresh()} fetching={summary.fetching} retryIn={summary.retryIn} />
    </div>
    {summary.refreshError ? <Banner tone="error" title="Refresh failed">{summary.refreshError}</Banner> : null}
    {data?.error ? <Banner tone="warn" title="Status source reported an error">{data.error}</Banner> : null}
    <div className="kpis"><KpiChip label="Active" value={active.length} tone="running" /><KpiChip label="Failed (unreviewed)" value={failed.filter((w) => !reviewed.has(w.workflow_id)).length} tone="failed" /></div>
    <section className="panel"><div className="panel-head"><h2>Running and pending</h2></div>
      <WorkflowTable workflows={active} loading={summary.loading} empty={<EmptyWorkflows message="Nothing is running or pending." />}
        actions={(w) => w.pending && w.workflow_id.startsWith('pending-') ? <span className="muted">dispatching</span>
          : <button type="button" className="btn btn-danger-outline" onClick={() => setPending({ kind: 'terminate', workflowId: w.workflow_id })}><Square size={12} />Stop</button>} />
    </section>
    <section className="panel"><div className="panel-head"><h2>Cancel a month</h2><RefreshButton onClick={() => void locks.reload()} fetching={locks.loading || locks.refreshing} label="Reload locks" /></div>
      {locks.loading ? <p className="muted"><Spinner /> Loading machine locks…</p> : Object.keys(locks.data?.busy_machines ?? {}).length === 0
        ? <p className="muted">No machine has an active backfill.</p>
        : <label className="field">Machine with an active backfill<select value={machineId} onChange={(e) => setMachineId(e.target.value)}>
          <option value="">Choose…</option>{Object.entries(locks.data?.busy_machines ?? {}).map(([id, info]) => <option key={id} value={id}>{id} · {info.source === 'argo_running' ? info.workflow_id ?? 'running' : 'control store'}</option>)}</select></label>}
      {machineId ? months.loading ? <p className="muted"><Spinner /> Loading months…</p> : months.error ? <ErrorState message={months.error} onRetry={() => void months.reload()} />
        : activeMonths.length ? <>
          <ul className="stage-list">{activeMonths.map((m) => { const t = workflowTone(m.state); return <li key={m.month_index}><strong className="mono">{monthLabel(m.year, m.month)}</strong>
            <div className="toolbar"><StatusBadge tone={t.tone} label={t.label} /><span className="mono muted">{m.child_workflow_id ?? ''}</span>
              <button type="button" className="btn btn-danger-outline" disabled={!['queued', 'running'].includes(m.state)} onClick={() => setPending({ kind: 'cancel', machineId, month: m })}>Cancel month</button></div></li>; })}</ul>
          {parent ? <button type="button" className="btn btn-danger" onClick={() => setPending({ kind: 'terminate', workflowId: parent })}><Square size={12} />Stop entire backfill ({parent})</button> : null}
        </> : <p className="muted">No active months for {machineId}.</p> : null}
    </section>
    <section className="panel"><div className="panel-head"><h2>Failures to review</h2></div>
      <WorkflowTable workflows={failed} empty={<EmptyState title="No failed workflows">in the last status snapshot.</EmptyState>}
        actions={(w) => <button type="button" className="btn" disabled={reviewBusy === w.workflow_id} onClick={() => void toggleReview(w, !reviewed.has(w.workflow_id))}>
          {reviewBusy === w.workflow_id ? <Spinner /> : null}{reviewed.has(w.workflow_id) ? 'Reviewed ✓ (undo)' : 'Mark reviewed'}</button>} />
    </section>
    <ConfirmDialog open={Boolean(pending)} danger busy={busy} error={actionError}
      title={pending?.kind === 'terminate' ? 'Stop workflow?' : 'Cancel this month?'}
      confirmLabel={pending?.kind === 'terminate' ? 'Stop workflow' : 'Cancel month'}
      confirmText={pending?.kind === 'terminate' ? pending.workflowId : undefined}
      onConfirm={() => void run()} onCancel={() => { setPending(null); setActionError(null); }}>
      {pending?.kind === 'terminate' ? <p>This terminates <code>{pending.workflowId}</code> in namespace {namespace}. Remaining months of a parent backfill stop too.</p>
        : pending ? <p>Stops {monthLabel(pending.month.year, pending.month.month)} for <code>{pending.machineId}</code>. The parent skips it and continues with the next scheduled month, or finishes cleanly.</p> : null}
    </ConfirmDialog>
  </>;
}
