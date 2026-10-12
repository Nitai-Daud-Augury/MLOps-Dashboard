import { ExternalLink, GitBranch, Workflow } from 'lucide-react';
import type { ReactNode } from 'react';
import { EmptyState, StatusBadge, Timestamp } from '../../components/ui';
import { DataTable, type Column } from '../../components/ui/DataTable';
import { duration } from '../../lib/format';
import { workflowTone } from '../../lib/status';
import type { RunningWorkflow } from '../../types';

const startedOf = (w: RunningWorkflow) => w.started_at ?? w.created_at ?? '';

/** Running/pending/finished workflows, paginated. Pending dashboard dispatches render dashed with their GHA link. */
export function WorkflowTable({ workflows, loading, empty, actions, prefix = 'wf', defaultSize = 25, label = 'Workflows' }: {
  workflows: RunningWorkflow[]; loading?: boolean; empty: ReactNode; actions?: (workflow: RunningWorkflow) => ReactNode; prefix?: string; defaultSize?: number; label?: string;
}) {
  const columns: Column<RunningWorkflow>[] = [
    { key: 'workflow', header: 'Workflow', cell: (workflow) => {
      const pending = Boolean(workflow.pending);
      return <div className="cell-stack"><span className="mono" style={{ color: 'var(--text)' }}>{pending && workflow.workflow_id.startsWith('pending-') ? 'pending… (dispatch)' : workflow.workflow_id}</span>
        <span className="muted wrap">{workflow.flow_name}{pending ? ` · ${workflow.pending_detail ?? 'not yet reported by GHA'}` : ''}</span></div>;
    }, sort: (a, b) => a.workflow_id.localeCompare(b.workflow_id) },
    { key: 'status', header: 'Status', cell: (w) => { const t = workflowTone(w.status); return <StatusBadge tone={t.tone} label={t.label} />; }, sort: (a, b) => a.status.localeCompare(b.status) },
    { key: 'machines', header: 'Machines', optional: true, cell: (w) => { const machines = w.machine_ids ?? []; return <span className="mono" title={machines.join(', ')}>{machines.length === 0 ? '—' : machines.length === 1 ? machines[0] : `${machines.length} machines`}</span>; } },
    { key: 'started', header: 'Started', cell: (w) => w.started_at ? <Timestamp value={w.started_at} /> : w.created_at ? <Timestamp value={w.created_at} prefix="created" /> : '—', sort: (a, b) => startedOf(a).localeCompare(startedOf(b)) },
    { key: 'duration', header: 'Duration', optional: true, cell: (w) => <span className="mono">{duration(w.started_at ?? w.created_at, w.finished_at)}</span> },
    { key: 'links', header: 'Links', cell: (workflow) => <span className="toolbar">
      {workflow.github_run_url ? <a href={workflow.github_run_url} target="_blank" rel="noreferrer" title="GitHub Actions run"><GitBranch size={13} /> GHA</a> : null}
      {workflow.outerbounds_url && workflow.outerbounds_url !== workflow.github_run_url ? <a href={workflow.outerbounds_url} target="_blank" rel="noreferrer" title="Outerbounds"><ExternalLink size={13} /> OB</a> : null}
    </span> },
    ...(actions ? [{ key: 'actions', header: <span className="sr-only">Actions</span>, cell: actions }] : []),
  ];
  if (!loading && !workflows.length) return <>{empty}</>;
  return <div className="table-wrap"><DataTable label={label} rows={workflows} columns={columns} rowKey={(w) => w.workflow_id} prefix={prefix} defaultSize={defaultSize}
    loading={loading} rowClassName={(w) => (w.pending ? 'pending-row' : undefined)} /></div>;
}

export function EmptyWorkflows({ message }: { message: string }) {
  return <EmptyState icon={<Workflow size={20} />} title="No workflows">{message}</EmptyState>;
}
