import { ArrowLeft } from 'lucide-react';
import { useEffect } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { EmptyState, ErrorState, RefreshButton, SkeletonRows, Spinner, StatusBadge, Timestamp } from '../../components/ui';
import { MachineFacts, MachineStatusBadge, MachineTitle } from '../../features/machines/MachineSummary';
import { MonthGrid } from '../../features/machines/MonthGrid';
import { phaseLabel } from '../../features/backfill/actions';
import { useWorkflowSummary } from '../../features/workflows/useWorkflowSummary';
import { workflowSort } from '../../features/workflows/sort';
import { WorkflowTable } from '../../features/workflows/WorkflowTable';
import { useResource } from '../../lib/api';
import { monthLabel } from '../../lib/format';
import { ACTIVE_MONTH_STATES, isFinishedWorkflow, workflowTone } from '../../lib/status';
import { useStatus } from '../../state/statusStore';
import type { ActiveMonth, AdminAction } from '../../types';
import { ScanFreshness } from './FleetPage';

const TABS = ['overview', 'coverage', 'running', 'history'] as const;
type MachineTab = typeof TABS[number];

export function MachinePage() {
  const { id = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const tab: MachineTab = (TABS as readonly string[]).includes(params.get('tab') ?? '') ? params.get('tab') as MachineTab : 'overview';
  const { payload, loading, error, reload } = useStatus();
  const machine = payload?.snapshot.machines.find((m) => m.machine_id === id);
  const active = useResource<{ months: ActiveMonth[] }>(`/api/machines/${encodeURIComponent(id)}/backfills/active`, { cacheKey: `active:${id}`, maxAgeMs: 30_000 });
  const activeMonths = (active.data?.months ?? []).filter((m) => ACTIVE_MONTH_STATES.includes(m.state));
  const summary = useWorkflowSummary({ live: tab === 'running' });
  const runningForMachine = (summary.data?.workflows ?? []).filter((w) => !isFinishedWorkflow(w.status) && (w.machine_ids ?? []).includes(id)).sort(workflowSort);
  const history = useResource<{ actions: AdminAction[] }>(tab === 'history' ? `/api/admin/actions?machine_id=${encodeURIComponent(id)}` : null, { cacheKey: `machine-actions:${id}`, maxAgeMs: 15_000 });
  const { reload: reloadActive } = active;
  const version = summary.data?.snapshot_version;
  useEffect(() => { if (version !== undefined) void reloadActive(); }, [version, reloadActive]);

  if (!payload && loading) return <div className="panel"><Spinner /> Loading machine…</div>;
  if (!payload && error) return <ErrorState message={error} onRetry={() => void reload()} />;
  if (!machine) return <EmptyState title={`Machine ${id} is not in the current scan`} action={<Link className="btn" to="/monitor">Back to fleet</Link>}>Check the ID, or rescan if it was added recently.</EmptyState>;

  const setTab = (next: MachineTab) => setParams(next === 'overview' ? {} : { tab: next }, { replace: true });
  const refreshRunning = async () => { await summary.refresh(); await reloadActive(); };
  return <>
    <div className="page-head">
      <div>
        <Link to="/monitor" className="muted"><ArrowLeft size={12} /> Fleet</Link>
        <h1><MachineTitle machine={machine} /></h1>
        <span><MachineStatusBadge machine={machine} /> <ScanFreshness payload={payload} /></span>
      </div>
    </div>
    <div className="tabs" role="tablist">
      {TABS.map((name) => <button key={name} type="button" role="tab" className="tab" aria-selected={tab === name} onClick={() => setTab(name)}>
        {name[0].toUpperCase() + name.slice(1)}{name === 'running' && (activeMonths.length || runningForMachine.length) ? ` ●${Math.max(activeMonths.length, runningForMachine.length)}` : ''}</button>)}
    </div>
    {tab === 'overview' ? <section className="panel"><MachineFacts machine={machine} /></section> : null}
    {tab === 'coverage' ? <section className="panel"><MonthGrid months={machine.months} active={activeMonths} /></section> : null}
    {tab === 'running' ? <section className="panel">
      <div className="panel-head"><h3>Running</h3>
        <span className="muted">{summary.data?.fetched_at ? <Timestamp value={summary.data.fetched_at} prefix="Status fetched" /> : 'Status not fetched yet'}</span>
        <RefreshButton onClick={() => void refreshRunning()} fetching={summary.fetching} retryIn={summary.retryIn} />
      </div>
      {summary.refreshError ? <p className="field-error">{summary.refreshError}</p> : null}
      <WorkflowTable workflows={runningForMachine} loading={summary.loading} empty={<p className="muted">No running or pending workflows for this machine.</p>} />
      <h3>Active months</h3>
      {active.loading ? <p className="muted"><Spinner /> Loading month locks…</p> : active.error ? <ErrorState message={active.error} onRetry={() => void reloadActive()} />
        : activeMonths.length ? <ul>{activeMonths.map((m) => { const t = workflowTone(m.state); return <li key={m.month_index}>
          <span className="mono">{monthLabel(m.year, m.month)}</span> <StatusBadge tone={t.tone} label={t.label} /> <span className="mono muted">{m.child_workflow_id ?? m.parent_workflow_id ?? ''}</span></li>; })}</ul>
          : <p className="muted">No active month locks.</p>}
    </section> : null}
    {tab === 'history' ? <section className="panel">
      <div className="panel-head"><h3>Dashboard runs for this machine</h3><RefreshButton onClick={() => void history.reload()} fetching={history.refreshing} /></div>
      {history.error && !history.data ? <ErrorState message={history.error} onRetry={() => void history.reload()} /> : <div className="table-wrap"><table>
        <thead><tr><th scope="col">Created</th><th scope="col">Action</th><th scope="col">Status</th><th scope="col">Workflow</th><th scope="col">Detail</th></tr></thead>
        <tbody>{history.loading ? <SkeletonRows columns={5} rows={3} /> : (history.data?.actions ?? []).map((action) => {
          const t = workflowTone(action.phase === 'accepted' ? action.status : action.phase ?? action.status);
          return <tr key={action.id}><td><Timestamp value={action.created_at} /></td><td>{action.flow_name ?? action.action}</td>
            <td><StatusBadge tone={t.tone} label={action.phase ? phaseLabel(action.phase) : t.label} /></td>
            <td className="mono">{action.outerbounds_url ? <a href={action.outerbounds_url} target="_blank" rel="noreferrer">{action.workflow_id ?? 'open'}</a> : action.workflow_id ?? '—'}</td>
            <td className="wrap muted">{action.error ?? ''}</td></tr>;
        })}</tbody></table></div>}
      {!history.loading && history.data && !history.data.actions.length ? <EmptyState title="No dashboard runs yet">Backfills started from Admin for this machine appear here.</EmptyState> : null}
    </section> : null}
  </>;
}
