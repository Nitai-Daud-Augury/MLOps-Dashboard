import { ExternalLink, GitBranch, Workflow } from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { QuickView } from '../../components/ui/QuickView';
import { shortRunId } from '../backfill/actionRecord';
import { monthLabel } from '../../lib/format';
import { EmptyState, StatusBadge, Timestamp } from '../../components/ui';
import { DataTable, type Column } from '../../components/ui/DataTable';
import { duration } from '../../lib/format';
import { workflowTone } from '../../lib/status';
import type { RunningWorkflow } from '../../types';

const machineHref = (id: string) => `/monitor/machines/${encodeURIComponent(id)}`;
const startedOf = (w: RunningWorkflow) => w.started_at ?? w.created_at ?? '';

/** Running/pending/finished workflows, paginated. Pending dashboard dispatches render dashed with their GHA link. */
export function WorkflowTable({ workflows, loading, empty, actions, prefix = 'wf', defaultSize = 25, label = 'Workflows' }: {
  workflows: RunningWorkflow[]; loading?: boolean; empty: ReactNode; actions?: (workflow: RunningWorkflow) => ReactNode; prefix?: string; defaultSize?: number; label?: string;
}) {
  const [viewing, setViewing] = useState<RunningWorkflow | null>(null);
  const columns: Column<RunningWorkflow>[] = [
    { key: 'workflow', header: 'Run', help: 'Opens the run in Outerbounds. Hover for the full id.', cell: (w) => {
      const pendingDispatch = Boolean(w.pending) && w.workflow_id.startsWith('pending-');
      const label = pendingDispatch ? 'dispatching…' : shortRunId(w.workflow_id) || w.workflow_id;
      return <span className={w.parent_workflow_id ? 'run-child' : undefined} title={`${w.workflow_id}${w.flow_name ? ` · ${w.flow_name}` : ''}`}>
        {w.parent_workflow_id ? <span className="muted" aria-label="child run">↳ </span> : null}
        {w.outerbounds_url && !pendingDispatch ? <a className="mono" href={w.outerbounds_url} target="_blank" rel="noreferrer">{label}</a> : <span className="mono">{label}</span>}
        {w.year && w.month ? <span className="muted"> · {monthLabel(w.year, w.month)}</span> : null}
        {w.pending ? <PendingNote workflow={w} /> : null}</span>;
    }, sort: (a, b) => a.workflow_id.localeCompare(b.workflow_id) },
    { key: 'status', header: 'Status', cell: (w) => { const t = workflowTone(w.status); return <StatusBadge tone={t.tone} label={t.label} />; }, sort: (a, b) => (a.status ?? '').localeCompare(b.status ?? '') },
    { key: 'machines', header: 'Machine', help: 'Opens the machine page. Quick view shows the run details here.', cell: (w) => { const machines = w.machine_ids ?? []; return <span className="machine-cell">
      {machines.length === 1 ? <Link className="mono" to={machineHref(machines[0])}>{machines[0]}</Link> : machines.length ? <span className="mono" title={machines.join(', ')}>{machines.length} machines</span> : <span className="muted">—</span>}
      <button type="button" className="btn btn-sm btn-ghost" onClick={() => setViewing(w)}>Quick view</button></span>; } },
    { key: 'started', header: 'Started', cell: (w) => w.started_at ? <Timestamp value={w.started_at} /> : w.created_at ? <Timestamp value={w.created_at} prefix="created" /> : '—', sort: (a, b) => startedOf(a).localeCompare(startedOf(b)) },
    { key: 'duration', header: 'Duration', optional: true, cell: (w) => <span className="mono">{duration(w.started_at ?? w.created_at, w.finished_at)}</span> },
    ...(actions ? [{ key: 'actions', header: <span className="sr-only">Actions</span>, cell: actions }] : []),
  ];
  if (!loading && !workflows.length) return <>{empty}</>;
  const ids = viewing?.machine_ids ?? [];
  return <div className="table-wrap"><DataTable label={label} rows={workflows} columns={columns} rowKey={(w) => w.workflow_id} prefix={prefix} defaultSize={defaultSize}
    loading={loading} rowClassName={(w) => (w.pending ? 'pending-row' : undefined)} />
    <QuickView open={Boolean(viewing)} onClose={() => setViewing(null)} title={viewing?.flow_name || 'Run'} subtitle={viewing ? workflowTone(viewing.status).label : ''}
      fields={viewing ? [
        { label: ids.length > 1 ? 'Machines' : 'Machine', value: ids.length ? <span className="mono">{ids.map((id, i) => <span key={id}>{i ? ', ' : ''}<Link to={machineHref(id)}>{id}</Link></span>)}</span> : '—' },
        { label: 'Run', value: <span className="mono">{viewing.workflow_id}</span> },
        ...(viewing.parent_workflow_id ? [{ label: 'Parent run', value: <span className="mono">{viewing.parent_workflow_id}</span> }] : []),
        ...(viewing.year && viewing.month ? [{ label: 'Month', value: monthLabel(viewing.year, viewing.month) }] : []),
        { label: 'Namespace', value: <span className="mono">{viewing.namespace || '—'}</span> },
        { label: 'Started', value: viewing.started_at ? <Timestamp value={viewing.started_at} /> : '—' },
        { label: 'Finished', value: viewing.finished_at ? <Timestamp value={viewing.finished_at} /> : '—' },
        ...(viewing.pending_detail ? [{ label: 'Pending', value: viewing.pending_detail }] : []),
      ] : []}
      actions={viewing ? <>
        {ids.length === 1 ? <Link className="btn" to={machineHref(ids[0])}>Machine page</Link> : null}
        {viewing.github_run_url ? <a className="btn" href={viewing.github_run_url} target="_blank" rel="noreferrer"><GitBranch size={14} />GitHub run</a> : null}
        {viewing.outerbounds_url ? <a className="btn btn-primary" href={viewing.outerbounds_url} target="_blank" rel="noreferrer">Open run <ExternalLink size={14} /></a> : null}
      </> : null} />
  </div>;
}

function PendingNote({ workflow }: { workflow: RunningWorkflow }) {
  return <span className="muted"> · {workflow.pending_detail ?? 'not yet reported'}</span>;
}

export function EmptyWorkflows({ message }: { message: string }) {
  return <EmptyState icon={<Workflow size={20} />} title="No workflows">{message}</EmptyState>;
}
