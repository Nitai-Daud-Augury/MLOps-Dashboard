import { useSearchParams } from 'react-router-dom';
import { Banner, ErrorState, KpiChip, RefreshButton, Timestamp } from '../../components/ui';
import { useToast } from '../../components/ui/toastContext';
import { useWorkflowSummary } from '../../features/workflows/useWorkflowSummary';
import { workflowSort } from '../../features/workflows/sort';
import { EmptyWorkflows, WorkflowTable } from '../../features/workflows/WorkflowTable';
import { isFinishedWorkflow } from '../../lib/status';

const VIEWS = { active: 'Active + pending', failed: 'Failed', done: 'Completed' } as const;
type View = keyof typeof VIEWS;

/** Live Ops: read-only workflow feed (cached snapshot + pending dispatch rows + push socket). */
export function LivePage() {
  const notify = useToast();
  const summary = useWorkflowSummary({ onLimited: (s) => notify('info', `Status refresh is rate limited: retry in ${s}s (showing the cached status).`) });
  const [params, setParams] = useSearchParams();
  const view = (params.get('show') as View) in VIEWS ? params.get('show') as View : 'active';
  const data = summary.data;
  const all = (data?.workflows ?? []).slice().sort(workflowSort);
  const lists: Record<View, typeof all> = {
    active: all.filter((w) => w.pending || !isFinishedWorkflow(w.status)),
    failed: all.filter((w) => ['failed', 'error'].includes(w.status.toLowerCase())),
    done: all.filter((w) => ['succeeded', 'completed'].includes(w.status.toLowerCase())),
  };
  if (!data && summary.error) return <ErrorState message={summary.error} onRetry={() => void summary.reload()} />;
  return <>
    <div className="page-head">
      <div><h1>Live Ops</h1>
        <span className="muted">
          {summary.socketSeen ? '● Live (socket connected)' : '○ Live updates when the server pushes'} · snapshot v{data?.snapshot_version ?? '—'} · {data?.fetched_at ? <Timestamp value={data.fetched_at} prefix="fetched" /> : 'status not fetched yet'}
        </span>
      </div>
      <RefreshButton onClick={() => void summary.refresh()} fetching={summary.fetching} retryIn={summary.retryIn} />
    </div>
    {summary.fetching ? <p className="muted" role="status">Fetching status… (on GitHub Actions a status refresh is a workflow run and takes a few minutes)</p> : null}
    {summary.refreshError ? <Banner tone="error" title="Refresh failed">{summary.refreshError}</Banner> : null}
    {data?.error ? <Banner tone="warn" title="Status source reported an error">{data.error}{data.hint ? ` ${data.hint}` : ''}</Banner> : null}
    <div className="kpis">
      <KpiChip label="Active" value={data?.counts?.active ?? lists.active.length} tone="running" />
      <KpiChip label="Pending dispatch" value={data?.pending_count ?? all.filter((w) => w.pending).length} tone="queued" />
      <KpiChip label="Failed" value={data?.counts?.failed ?? lists.failed.length} tone={lists.failed.length ? 'failed' : undefined} />
      <KpiChip label="Done" value={data?.counts?.completed ?? lists.done.length} tone="success" />
    </div>
    <div className="tabs" role="tablist">{(Object.keys(VIEWS) as View[]).map((key) => <button key={key} type="button" role="tab" className="tab" aria-selected={view === key}
      onClick={() => setParams(key === 'active' ? {} : { show: key }, { replace: true })}>{VIEWS[key]} ({lists[key].length})</button>)}</div>
    <WorkflowTable workflows={lists[view]} loading={summary.loading} empty={<EmptyWorkflows message={view === 'active' ? 'Nothing is running or pending. New dashboard dispatches show up here immediately as pending rows.' : 'No workflows in this list in the last status snapshot.'} />} />
  </>;
}
