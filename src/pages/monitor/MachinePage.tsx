import { ArrowLeft, CalendarCheck, CalendarX, Clock, Loader } from 'lucide-react';
import { useEffect } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { EmptyState, ErrorState, RefreshButton, Spinner, StatusBadge, Timestamp } from '../../components/ui';
import { DataTable, type Column } from '../../components/ui/DataTable';
import { KpiCard, PageHeader, Section } from '../../components/ui/Layout';
import { MachineFacts, MachineStatusBadge, MachineTitle } from '../../features/machines/MachineSummary';
import { MonthGrid } from '../../features/machines/MonthGrid';
import { phaseLabel } from '../../features/backfill/actions';
import { useWorkflowSummary } from '../../features/workflows/useWorkflowSummary';
import { nestChildren, workflowSort } from '../../features/workflows/sort';
import { WorkflowTable } from '../../features/workflows/WorkflowTable';
import { useResource } from '../../lib/api';
import { monthLabel } from '../../lib/format';
import { ACTIVE_MONTH_STATES, isFinishedWorkflow, workflowTone } from '../../lib/status';
import { useStatus } from '../../state/statusStore';
import type { ActiveMonth, AdminAction } from '../../types';
import { ScanFreshness } from './FleetPage';

const TABS = ['overview', 'coverage', 'running', 'history'] as const;
type MachineTab = typeof TABS[number];

const HISTORY: Column<AdminAction>[] = [
  { key: 'created', header: 'Created', cell: (a) => <Timestamp value={a.created_at} />, sort: (a, b) => a.created_at.localeCompare(b.created_at) },
  { key: 'action', header: 'Action', cell: (a) => a.flow_name ?? a.action },
  { key: 'status', header: 'Status', cell: (a) => { const t = workflowTone(a.phase === 'accepted' ? a.status : a.phase ?? a.status); return <StatusBadge tone={t.tone} label={a.phase ? phaseLabel(a.phase) : t.label} />; } },
  { key: 'workflow', header: 'Workflow', cell: (a) => <span className="mono">{a.outerbounds_url ? <a href={a.outerbounds_url} target="_blank" rel="noreferrer">{a.workflow_id ?? 'open'}</a> : a.workflow_id ?? '—'}</span> },
  { key: 'detail', header: 'Detail', optional: true, cell: (a) => <span className="wrap muted">{a.error ?? ''}</span> },
];

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

  if (!payload && loading) return <div className="card section"><span><Spinner /> Loading machine…</span></div>;
  if (!payload && error) return <ErrorState message={error} onRetry={() => void reload()} />;
  if (!machine) return <EmptyState title={`Machine ${id} is not in the current scan`} action={<Link className="btn" to="/monitor">Back to fleet</Link>}>Check the ID, or rescan if it was added recently.</EmptyState>;

  const setTab = (next: MachineTab) => setParams(next === 'overview' ? {} : { tab: next }, { replace: true });
  const refreshRunning = async () => { await summary.refresh(); await reloadActive(); };
  const missing = Math.max(0, machine.months_expected - machine.months_complete);
  const runningCount = Math.max(activeMonths.length, runningForMachine.length);
  return <>
    <Link to="/monitor" className="muted" style={{ display: 'inline-flex', gap: 4, alignItems: 'center', textDecoration: 'none' }}><ArrowLeft size={14} /> Fleet coverage</Link>
    <PageHeader title={<MachineTitle machine={machine} />} purpose="One machine's month-by-month feature coverage and the dashboard runs that touched it."
      meta={<><MachineStatusBadge machine={machine} /><ScanFreshness payload={payload} /></>} />
    <div className="kpis">
      <KpiCard size="sm" icon={<CalendarCheck size={18} />} tone="ok" label="Months present" value={`${machine.months_complete}/${machine.months_expected}`} helper="Expected months with features" />
      <KpiCard size="sm" icon={<CalendarX size={18} />} tone={missing ? 'warn' : 'neutral'} label="Missing months" value={missing} helper={machine.first_missing_month ? `First missing ${machine.first_missing_month}` : 'None missing'} />
      <KpiCard size="sm" icon={<Clock size={18} />} tone="neutral" label="Last populated" value={<span className="mono" style={{ fontSize: 18 }}>{machine.last_populated_month ?? '—'}</span>} helper="Latest month with features" />
      <KpiCard size="sm" icon={<Loader size={18} />} tone={runningCount ? 'primary' : 'neutral'} label="Running now" value={runningCount} helper="Active months or workflows" onClick={() => setTab('running')} active={tab === 'running'} />
    </div>
    <div className="tabs" role="tablist">
      {TABS.map((name) => <button key={name} type="button" role="tab" className="tab" aria-selected={tab === name} onClick={() => setTab(name)}>
        {name[0].toUpperCase() + name.slice(1)}{name === 'running' && runningCount ? <span className="nav-badge" style={{ marginLeft: 6 }}>{runningCount}</span> : null}</button>)}
    </div>
    {tab === 'overview' ? <Section title="Overview" description="Key facts from the last scan: coverage, lifecycle and the recommended next step."><MachineFacts machine={machine} /></Section> : null}
    {tab === 'coverage' ? <Section title="Month coverage" description="One cell per expected month. Icons and tooltips explain each state; running and queued months come from the live month locks.">
      <MonthGrid months={machine.months} active={activeMonths} /></Section> : null}
    {tab === 'running' ? <>
      <Section title="Running workflows" description="Workflows for this machine that have not finished, including pending dashboard dispatches."
        actions={<><span className="muted">{summary.data?.fetched_at ? <Timestamp value={summary.data.fetched_at} prefix="Status fetched" /> : 'Status not fetched yet'}</span>
          <RefreshButton onClick={() => void refreshRunning()} fetching={summary.fetching} retryIn={summary.retryIn} /></>}>
        {summary.refreshError ? <p className="field-error">{summary.refreshError}</p> : null}
        <WorkflowTable workflows={nestChildren(runningForMachine)} loading={summary.loading} defaultSize={10} empty={<p className="muted">No running or pending workflows for this machine.</p>} />
      </Section>
      <Section title="Active months" description="Month locks held by a running backfill for this machine.">
        {active.loading ? <p className="muted"><Spinner /> Loading month locks…</p> : active.error ? <ErrorState message={active.error} onRetry={() => void reloadActive()} />
          : activeMonths.length ? <ul className="stage-list">{activeMonths.map((m) => { const t = workflowTone(m.state); return <li key={m.month_index}>
            <strong className="mono">{monthLabel(m.year, m.month)}</strong><div className="toolbar"><StatusBadge tone={t.tone} label={t.label} /><span className="mono muted">{m.child_workflow_id ?? m.parent_workflow_id ?? ''}</span></div></li>; })}</ul>
            : <p className="muted">No active month locks.</p>}
      </Section>
    </> : null}
    {tab === 'history' ? <Section flush title="Dashboard runs" description="Backfills and stops started from Admin for this machine, newest first."
      actions={<RefreshButton onClick={() => void history.reload()} fetching={history.refreshing} />}>
      {history.error && !history.data ? <ErrorState message={history.error} onRetry={() => void history.reload()} />
        : <DataTable label="Dashboard runs" prefix="h" defaultSize={10} rows={history.data?.actions ?? []} columns={HISTORY} rowKey={(a) => a.id} loading={history.loading}
          empty={<EmptyState title="No dashboard runs yet">Backfills started from Admin for this machine appear here.</EmptyState>} />}
    </Section> : null}
  </>;
}
