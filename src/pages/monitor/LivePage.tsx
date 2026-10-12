import { Activity, CheckCircle2, Radio, Send, XCircle } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { Banner, ErrorState, RefreshButton, Timestamp } from '../../components/ui';
import { KpiCard, PageHeader, Section, type KpiState } from '../../components/ui/Layout';
import { useToast } from '../../components/ui/toastContext';
import { useWorkflowSummary } from '../../features/workflows/useWorkflowSummary';
import { workflowSort } from '../../features/workflows/sort';
import { DONE, FAILED, finishedWithin } from '../../features/workflows/windowed';
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
    failed: all.filter((w) => FAILED.includes(w.status.toLowerCase())),
    done: all.filter((w) => DONE.includes(w.status.toLowerCase())),
  };
  const failed24 = finishedWithin(all, FAILED).length;
  const done24 = finishedWithin(all, DONE).length;
  const pending = data?.pending_count ?? all.filter((w) => w.pending).length;
  const state: KpiState = summary.loading ? 'loading' : !data && summary.error ? 'error' : summary.refreshError || data?.error ? 'stale' : 'ready';
  const show = (key: View) => setParams(key === 'active' ? {} : { show: key }, { replace: true });
  if (!data && summary.error) return <ErrorState message={summary.error} onRetry={() => void summary.reload()} />;
  return <>
    <PageHeader title="Live Ops" purpose="What is running right now and what finished recently. Read-only; to stop a run, use Admin → Operations."
      meta={<>
        <span className={`badge ${summary.socketSeen ? 'tone-success' : 'tone-na'}`}><Radio size={12} aria-hidden="true" />{summary.socketSeen ? 'Live (socket connected)' : 'Live updates when the server pushes'}</span>
        <span>snapshot v{data?.snapshot_version ?? '—'}</span>
        <span>{data?.fetched_at ? <Timestamp value={data.fetched_at} prefix="fetched" /> : 'status not fetched yet'}</span>
      </>}
      actions={<RefreshButton onClick={() => void summary.refresh()} fetching={summary.fetching} retryIn={summary.retryIn} />} />
    {summary.fetching ? <Banner tone="info" title="Fetching status…">On GitHub Actions a status refresh is a workflow run and takes a few minutes; cached rows stay visible.</Banner> : null}
    {summary.refreshError ? <Banner tone="error" title="Refresh failed">{summary.refreshError}</Banner> : null}
    {data?.error ? <Banner tone="warn" title="Status source reported an error">{data.error}{data.hint ? ` ${data.hint}` : ''}</Banner> : null}
    <div className="kpis">
      <KpiCard icon={<Activity size={20} />} tone="primary" label="Active" value={lists.active.filter((w) => !w.pending).length} state={state}
        helper="Running or queued workflows" tooltip="Workflows the status source reports as not finished." active={view === 'active'} onClick={() => show('active')} />
      <KpiCard icon={<Send size={20} />} tone="violet" label="Pending dispatch" value={pending} state={state}
        helper="Sent, not yet reported" tooltip="Dashboard dispatches the status source has not picked up yet (shown as dashed rows)." onClick={() => show('active')} />
      <KpiCard icon={<XCircle size={20} />} tone={failed24 ? 'danger' : 'neutral'} label="Failed (24h)" value={failed24} state={state}
        helper={`${lists.failed.length} failed in snapshot`} tooltip="Failed workflows whose finish time is within the last 24 hours (computed in the browser)." active={view === 'failed'} onClick={() => show('failed')} />
      <KpiCard icon={<CheckCircle2 size={20} />} tone="ok" label="Completed (24h)" value={done24} state={state}
        helper={`${lists.done.length} completed in snapshot`} tooltip="Succeeded workflows whose finish time is within the last 24 hours (computed in the browser)." active={view === 'done'} onClick={() => show('done')} />
    </div>
    <Section title="Workflows" description="Pending rows are dispatches the dashboard sent that the status source has not reported yet. Switch lists with the tabs or the cards.">
      <div className="tabs" role="tablist">{(Object.keys(VIEWS) as View[]).map((key) => <button key={key} type="button" role="tab" className="tab" aria-selected={view === key}
        onClick={() => show(key)}>{VIEWS[key]} <span className="muted num">{lists[key].length}</span></button>)}</div>
      <WorkflowTable workflows={lists[view]} loading={summary.loading} empty={<EmptyWorkflows message={view === 'active' ? 'Nothing is running or pending. New dashboard dispatches show up here immediately as pending rows.' : 'No workflows in this list in the last status snapshot.'} />} />
    </Section>
  </>;
}
