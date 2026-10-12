import { Link } from 'react-router-dom';
import { Banner, EmptyState, ErrorState, RefreshButton, SkeletonRows, StatusBadge, Timestamp } from '../../components/ui';
import { phaseLabel } from '../../features/backfill/actions';
import { useResource } from '../../lib/api';
import { workflowTone } from '../../lib/status';
import { useStatus } from '../../state/statusStore';
import type { AdminAction, AdminReadiness, AdminReadinessCheck } from '../../types';

function Check({ name, check, loading }: { name: string; check?: AdminReadinessCheck; loading: boolean }) {
  const tone = loading ? 'queued' : check?.ready ? 'success' : check?.error_type === 'outerbounds_sp_not_configured' ? 'warn' : 'failed';
  return <li><strong>{name}</strong><div>
    <StatusBadge tone={tone} label={loading ? 'Checking…' : check?.ready ? 'Ready' : 'Not ready'} />
    {check?.backend ? <span className="mono muted"> {check.backend}</span> : null}
    {check?.account_name ? <span className="mono muted"> {check.account_name}/{check.container_name}</span> : null}
    {check?.detail ? <p className="wrap muted">{check.detail}</p> : null}
  </div></li>;
}

export function AdminOverviewPage() {
  const { runtime } = useStatus();
  const readiness = useResource<AdminReadiness>('/api/admin/readiness', { cacheKey: 'admin-readiness', maxAgeMs: 5 * 60_000 });
  const actions = useResource<{ actions: AdminAction[] }>('/api/admin/actions', { cacheKey: 'admin-actions', maxAgeMs: 60_000 });
  const recent = (actions.data?.actions ?? []).slice(0, 25);
  return <>
    <div className="page-head"><div><h1>Admin overview</h1><span className="muted">Readiness checks and recent dashboard actions.</span></div>
      <div className="toolbar"><Link className="btn btn-primary" to="/admin/backfill">New backfill</Link></div></div>
    {runtime?.workflow_mutations_error ? <Banner tone="warn" title="Workflow actions will be refused by the server">{runtime.workflow_mutations_error}</Banner> : null}
    <section className="panel">
      <div className="panel-head"><h2>Readiness</h2><RefreshButton onClick={() => void readiness.reload()} fetching={readiness.loading || readiness.refreshing} label="Re-check" /></div>
      {readiness.error && !readiness.data ? <ErrorState message={readiness.error} onRetry={() => void readiness.reload()} /> :
        <ul className="stage-list">
          <Check name="Manifest storage" check={readiness.data?.manifest_storage} loading={!readiness.data} />
          <Check name="Workflow backend" check={readiness.data?.argo} loading={!readiness.data} />
        </ul>}
    </section>
    <section className="panel">
      <div className="panel-head"><h2>Recent actions</h2><RefreshButton onClick={() => void actions.reload()} fetching={actions.refreshing} /></div>
      {actions.error && !actions.data ? <ErrorState message={actions.error} onRetry={() => void actions.reload()} /> : <div className="table-wrap"><table>
        <thead><tr><th scope="col">Created</th><th scope="col">Action</th><th scope="col">Status</th><th scope="col">Machines</th><th scope="col">Workflow</th><th scope="col">Error</th></tr></thead>
        <tbody>{actions.loading ? <SkeletonRows columns={6} rows={4} /> : recent.map((action) => {
          const t = workflowTone(action.phase && action.phase !== 'accepted' ? action.phase : action.status);
          return <tr key={action.id}>
            <td><Timestamp value={action.created_at} /></td>
            <td>{action.action}{action.flow_name ? <span className="muted"> · {action.flow_name}</span> : null}</td>
            <td><StatusBadge tone={t.tone} label={action.phase ? phaseLabel(action.phase) : t.label} /></td>
            <td className="mono" title={(action.machine_ids ?? []).join(', ')}>{action.machine_ids?.length ? (action.machine_ids.length === 1 ? action.machine_ids[0] : `${action.machine_ids.length} machines`) : '—'}</td>
            <td className="mono">{action.outerbounds_url ? <a href={action.outerbounds_url} target="_blank" rel="noreferrer">{action.workflow_id ?? 'open'}</a> : action.workflow_id ?? '—'}</td>
            <td className="wrap muted">{action.error ?? ''}</td>
          </tr>;
        })}</tbody></table></div>}
      {!actions.loading && actions.data && !recent.length ? <EmptyState title="No admin actions yet" action={<Link className="btn" to="/admin/backfill">Start a backfill</Link>}>Triggers, stops and probes are recorded here.</EmptyState> : null}
    </section>
  </>;
}
