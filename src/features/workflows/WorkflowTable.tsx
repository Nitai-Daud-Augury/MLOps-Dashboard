import { ExternalLink, GitBranch } from 'lucide-react';
import type { ReactNode } from 'react';
import { EmptyState, SkeletonRows, StatusBadge, Timestamp } from '../../components/ui';
import { duration } from '../../lib/format';
import { workflowTone } from '../../lib/status';
import type { RunningWorkflow } from '../../types';

/** Running/pending/finished workflows. Pending dashboard dispatches render dashed with their GHA link. */
export function WorkflowTable({ workflows, loading, empty, actions }: {
  workflows: RunningWorkflow[]; loading?: boolean; empty: ReactNode; actions?: (workflow: RunningWorkflow) => ReactNode;
}) {
  if (!loading && !workflows.length) return <>{empty}</>;
  return <div className="table-wrap"><table>
    <thead><tr>
      <th scope="col">Workflow</th><th scope="col">Status</th><th scope="col">Machines</th><th scope="col">Started</th><th scope="col">Duration</th><th scope="col">Links</th>
      {actions ? <th scope="col"><span className="sr-only">Actions</span></th> : null}
    </tr></thead>
    <tbody>
      {loading && !workflows.length ? <SkeletonRows columns={actions ? 7 : 6} rows={4} /> : workflows.map((workflow) => {
        const pending = Boolean(workflow.pending);
        const { tone, label } = workflowTone(workflow.status);
        const machines = workflow.machine_ids ?? [];
        return <tr key={workflow.workflow_id} className={pending ? 'pending-row' : undefined}>
          <td><div className="mono">{pending && workflow.workflow_id.startsWith('pending-') ? 'pending… (dispatch)' : workflow.workflow_id}</div>
            <div className="muted wrap">{workflow.flow_name}{pending ? ` · ${workflow.pending_detail ?? 'not yet reported by GHA'}` : ''}</div></td>
          <td><StatusBadge tone={tone} label={label} /></td>
          <td className="mono" title={machines.join(', ')}>{machines.length === 0 ? '—' : machines.length === 1 ? machines[0] : `${machines.length} machines`}</td>
          <td>{workflow.started_at ? <Timestamp value={workflow.started_at} /> : workflow.created_at ? <Timestamp value={workflow.created_at} prefix="created" /> : '—'}</td>
          <td className="mono">{duration(workflow.started_at ?? workflow.created_at, workflow.finished_at)}</td>
          <td><span className="toolbar">
            {workflow.github_run_url ? <a href={workflow.github_run_url} target="_blank" rel="noreferrer" title="GitHub Actions run"><GitBranch size={13} /> GHA</a> : null}
            {workflow.outerbounds_url && workflow.outerbounds_url !== workflow.github_run_url ? <a href={workflow.outerbounds_url} target="_blank" rel="noreferrer" title="Outerbounds"><ExternalLink size={13} /> OB</a> : null}
          </span></td>
          {actions ? <td>{actions(workflow)}</td> : null}
        </tr>;
      })}
    </tbody>
  </table></div>;
}

export function EmptyWorkflows({ message }: { message: string }) {
  return <EmptyState title="No workflows">{message}</EmptyState>;
}
