import { CalendarClock, ExternalLink, ListChecks, Plus, Send, XCircle } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { QuickView } from '../../components/ui/QuickView';
import { actionError, actionMachines, shortRunId } from '../../features/backfill/actionRecord';
import { Banner, EmptyState, ErrorState, RefreshButton, StatusBadge, Timestamp } from '../../components/ui';
import { DataTable, type Column } from '../../components/ui/DataTable';
import { KpiCard, PageHeader, Section } from '../../components/ui/Layout';
import { phaseLabel } from '../../features/backfill/actions';
import { useWorkflowSummary } from '../../features/workflows/useWorkflowSummary';
import { useResource } from '../../lib/api';
import { workflowTone } from '../../lib/status';
import { useStatus } from '../../state/statusStore';
import type { AdminAction, AdminReadiness, AdminReadinessCheck } from '../../types';

const IL_DAY = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Jerusalem', year: 'numeric', month: '2-digit', day: '2-digit' });
const ilDay = (value: string | number) => IL_DAY.format(new Date(value));
const isFailed = (a: AdminAction) => a.status === 'failed' || a.phase === 'failed';

function Check({ name, what, check, loading }: { name: string; what: string; check?: AdminReadinessCheck; loading: boolean }) {
  const tone = loading ? 'queued' : check?.ready ? 'success' : check?.error_type === 'outerbounds_sp_not_configured' ? 'warn' : 'failed';
  return <li><div className="cell-stack"><strong>{name}</strong><span className="helper">{what}</span></div><div>
    <StatusBadge tone={tone} label={loading ? 'Checking…' : check?.ready ? 'Ready' : 'Not ready'} />
    {check?.backend ? <span className="mono muted"> {check.backend}</span> : null}
    {check?.account_name ? <span className="mono muted"> {check.account_name}/{check.container_name}</span> : null}
    {check?.detail ? <p className="wrap muted">{check.detail}</p> : null}
  </div></li>;
}

const machineHref = (id: string) => `/monitor/machines/${encodeURIComponent(id)}`;

/** One-line error; full text on hover, click to expand/collapse. */
function ErrorCell({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  return <button type="button" className={`error-cell${open ? ' open' : ''}`} title={open ? 'Click to collapse' : text} aria-expanded={open} onClick={() => setOpen(!open)}>{text}</button>;
}

function actionColumns(onQuickView: (a: AdminAction) => void): Column<AdminAction>[] {
  return [
    { key: 'created', header: 'Created', cell: (a) => <Timestamp value={a.created_at} />, sort: (a, b) => a.created_at.localeCompare(b.created_at) },
    { key: 'action', header: 'Action', cell: (a) => a.action },
    { key: 'status', header: 'Status', cell: (a) => { const t = workflowTone(a.phase && a.phase !== 'accepted' ? a.phase : a.status); return <StatusBadge tone={t.tone} label={a.phase ? phaseLabel(a.phase) : t.label} />; } },
    { key: 'machine', header: 'Machine', help: 'Opens the machine page. Quick view shows the run details here.', cell: (a) => { const ids = actionMachines(a); return <span className="machine-cell">
      {ids.length === 1 ? <Link className="mono" to={machineHref(ids[0])}>{ids[0]}</Link> : ids.length ? <span className="mono" title={ids.join(', ')}>{ids.length} machines</span> : <span className="muted">—</span>}
      <button type="button" className="btn btn-sm btn-ghost" onClick={() => onQuickView(a)}>Quick view</button></span>; } },
    { key: 'workflow', header: 'Run', help: 'Opens the run in Outerbounds.', cell: (a) => a.outerbounds_url
      ? <a className="mono" href={a.outerbounds_url} target="_blank" rel="noreferrer" title={a.workflow_id ?? a.flow_name ?? ''}>{shortRunId(a.workflow_id) || 'All runs'}</a>
      : <span className="mono muted">{shortRunId(a.workflow_id) || '—'}</span> },
    { key: 'error', header: 'Error', optional: true, cell: (a) => <ErrorCell text={actionError(a)} /> },
  ];
}

export function AdminOverviewPage() {
  const { runtime } = useStatus();
  const readiness = useResource<AdminReadiness>('/api/admin/readiness', { cacheKey: 'admin-readiness', maxAgeMs: 5 * 60_000 });
  const actions = useResource<{ actions: AdminAction[] }>('/api/admin/actions', { cacheKey: 'admin-actions', maxAgeMs: 60_000 });
  const summary = useWorkflowSummary({ live: false });
  const list = actions.data?.actions ?? [];
  const [viewing, setViewing] = useState<AdminAction | null>(null);
  const columns = useMemo(() => actionColumns(setViewing), []);
  const viewIds = viewing ? actionMachines(viewing) : [];
  const today = ilDay(Date.now());
  const todayCount = list.filter((a) => ilDay(a.created_at) === today).length;
  const failed24 = list.filter((a) => isFailed(a) && Date.now() - Date.parse(a.finished_at ?? a.created_at) <= 24 * 3600_000).length;
  const checks = [readiness.data?.manifest_storage, readiness.data?.argo];
  const ready = checks.filter((c) => c?.ready).length;
  const pending = summary.data?.pending_count ?? (summary.data?.workflows ?? []).filter((w) => w.pending).length;
  const actionsState = actions.loading ? 'loading' : actions.error && !actions.data ? 'error' : 'ready';
  return <>
    <PageHeader title="Admin overview" purpose="Is the dashboard able to dispatch work, and what did it do recently?"
      actions={<Link className="btn btn-primary" to="/admin/backfill"><Plus size={14} />New backfill</Link>} />
    {runtime?.workflow_mutations_error ? <Banner tone="warn" title="Workflow actions will be refused by the server">{runtime.workflow_mutations_error}</Banner> : null}
    <div className="kpis">
      <KpiCard icon={<ListChecks size={20} />} tone={!readiness.data ? 'neutral' : ready === 2 ? 'ok' : 'danger'} label="Readiness" value={`${ready}/2`}
        state={readiness.loading ? 'loading' : readiness.error && !readiness.data ? 'error' : 'ready'} helper={ready === 2 ? 'All checks pass' : 'See the checks below'} tooltip="Manifest storage and workflow backend checks." />
      <KpiCard icon={<Send size={20} />} tone="violet" label="Pending dispatches" value={pending} state={summary.loading ? 'loading' : !summary.data && summary.error ? 'error' : 'ready'}
        helper="Sent, not yet reported" tooltip="Dashboard dispatches the status source has not reported yet (workflow summary pending_count)." />
      <KpiCard icon={<CalendarClock size={20} />} tone="admin" label="Actions today" value={todayCount} state={actionsState}
        helper="Since midnight (IL)" tooltip="Admin actions created today, Asia/Jerusalem calendar day (from created_at)." />
      <KpiCard icon={<XCircle size={20} />} tone={failed24 ? 'danger' : 'neutral'} label="Failed actions (24h)" value={failed24} state={actionsState}
        helper={failed24 ? 'Review in the table below' : 'None'} tooltip="Admin actions with status or phase failed in the last 24 hours." />
    </div>
    <Section title="Readiness" description="Checks that must pass before a backfill can be dispatched. Re-check after fixing credentials or configuration."
      actions={<RefreshButton onClick={() => void readiness.reload()} fetching={readiness.loading || readiness.refreshing} label="Re-check" />}>
      {readiness.error && !readiness.data ? <ErrorState message={readiness.error} onRetry={() => void readiness.reload()} /> :
        <ul className="stage-list">
          <Check name="Manifest storage" what="Where backfill manifests are written" check={readiness.data?.manifest_storage} loading={!readiness.data} />
          <Check name="Workflow backend" what="The system that runs the backfill flows" check={readiness.data?.argo} loading={!readiness.data} />
        </ul>}
    </Section>
    <Section flush title="Recent actions" description="Every trigger, stop and probe sent from this dashboard, newest first."
      actions={<RefreshButton onClick={() => void actions.reload()} fetching={actions.refreshing} />}>
      {actions.error && !actions.data ? <ErrorState message={actions.error} onRetry={() => void actions.reload()} />
        : <DataTable label="Recent actions" prefix="a" defaultSize={10} rows={list} columns={columns} rowKey={(a) => a.id} loading={actions.loading}
          rowClassName={(a) => (isFailed(a) ? 'failed' : undefined)}
          empty={<EmptyState title="No admin actions yet" action={<Link className="btn" to="/admin/backfill">Start a backfill</Link>}>Triggers, stops and probes are recorded here.</EmptyState>} />}
    </Section>
    <QuickView open={Boolean(viewing)} onClose={() => setViewing(null)} title={viewing?.flow_name ?? viewing?.action ?? ''} subtitle={viewing ? `${viewing.action} · ${viewing.status}` : ''}
      fields={viewing ? [
        { label: viewIds.length > 1 ? 'Machines' : 'Machine', value: viewIds.length ? <span className="mono">{viewIds.map((id, i) => <span key={id}>{i ? ', ' : ''}<Link to={machineHref(id)}>{id}</Link></span>)}</span> : '—' },
        { label: 'Run', value: <span className="mono">{viewing.workflow_id ?? '—'}</span> },
        { label: 'Created', value: <Timestamp value={viewing.created_at} /> },
        { label: 'Finished', value: viewing.finished_at ? <Timestamp value={viewing.finished_at} /> : '—' },
        ...(viewing.split?.mode ? [{ label: 'Split', value: `${viewing.split.mode}${viewing.split.days ? ` · ${viewing.split.days} days` : ''}` }] : []),
        ...(actionError(viewing) ? [{ label: 'Error', value: <span className="field-error">{actionError(viewing)}</span> }] : []),
      ] : []}
      actions={viewing ? <>
        {viewIds.length === 1 ? <Link className="btn" to={machineHref(viewIds[0])}>Machine page</Link> : null}
        {viewing.outerbounds_url ? <a className="btn btn-primary" href={viewing.outerbounds_url} target="_blank" rel="noreferrer">Open run <ExternalLink size={14} /></a> : null}
      </> : null} />
  </>;
}
