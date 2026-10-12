import { Download, Play, RotateCcw, X } from 'lucide-react';
import { useMemo } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { API_BASE, STATUS_OPTIONS } from '../../constants';
import { EmptyState, ErrorState, KpiChip, RefreshButton, SkeletonRows, Spinner, Timestamp } from '../../components/ui';
import { MachineFacts, MachineStatusBadge, MachineTitle } from '../../features/machines/MachineSummary';
import { MonthGrid } from '../../features/machines/MonthGrid';
import { duration, exactTime } from '../../lib/format';
import { BACKFILL_STATUS } from '../../lib/status';
import { useStatus } from '../../state/statusStore';
import type { MachineStatus, StatusPayload } from '../../types';

const PAGE_SIZE = 50;
type SortKey = 'machine' | 'status' | 'coverage' | 'first_missing' | 'last_populated';
const SORTERS: Record<SortKey, (a: MachineStatus, b: MachineStatus) => number> = {
  machine: (a, b) => (a.display_name || a.machine_id).localeCompare(b.display_name || b.machine_id),
  status: (a, b) => a.status.localeCompare(b.status),
  coverage: (a, b) => (a.months_complete / (a.months_expected || 1)) - (b.months_complete / (b.months_expected || 1)),
  first_missing: (a, b) => (a.first_missing_month ?? '9999').localeCompare(b.first_missing_month ?? '9999'),
  last_populated: (a, b) => (a.last_populated_month ?? '').localeCompare(b.last_populated_month ?? ''),
};

/** "Last scanned" slot: restore state after an app restart, else the scan time. */
export function ScanFreshness({ payload }: { payload: StatusPayload | null }) {
  if (!payload) return <span className="muted"><Spinner /> Loading status…</span>;
  const persistence = payload.persistence;
  if (persistence?.loading) return <span className="muted" role="status"><Spinner /> Loading saved scan…</span>;
  const generated = payload.snapshot.machines.length ? payload.snapshot.generated_at : null;
  if (!generated) return <span className="muted">No scan yet — run a scan to compute coverage.</span>;
  return <span>Last scanned <span className="mono" title="Asia/Jerusalem">{exactTime(generated)}</span> (<Timestamp value={generated} />)
    {persistence?.loaded_from ? <span className="badge tone-na" title="These results were restored from the saved scan after an app restart. Rescan to refresh them.">restored after restart</span> : null}
    {payload.snapshot.features_source ? <span className="muted"> · from {payload.snapshot.features_source}</span> : null}
  </span>;
}

function ScanProgress({ payload }: { payload: StatusPayload }) {
  const state = payload.scan_state;
  const total = state.total_machines || 0;
  const pct = total ? Math.round((state.completed_machines / total) * 100) : 0;
  return <div className="progress" role="status" aria-live="polite">
    <span>Scanning {total ? `${pct}% · ${state.completed_machines}/${total} machines` : `(${state.phase ?? 'starting'})`} · {duration(state.started_at)} elapsed</span>
    <div className="progress-track" aria-hidden="true"><div className="progress-fill" style={{ width: `${total ? pct : 5}%` }} /></div>
  </div>;
}

export function FleetPage() {
  const { payload, loading, error, refreshing, reload, startScan, resetScan, scanRequesting } = useStatus();
  const [params, setParams] = useSearchParams();
  const query = params.get('q') ?? '';
  const status = params.get('status') ?? 'all';
  const hideTest = params.get('hide_test') === '1';
  const sort = (params.get('sort') as SortKey) || 'status';
  const dir = params.get('dir') === 'desc' ? -1 : 1;
  const page = Math.max(1, Number(params.get('page') ?? 1) || 1);
  const quick = params.get('quick');
  const update = (changes: Record<string, string | null>) => setParams((current) => {
    const next = new URLSearchParams(current);
    for (const [key, value] of Object.entries(changes)) { if (value === null || value === '') next.delete(key); else next.set(key, value); }
    if (!('page' in changes)) next.delete('page');
    return next;
  }, { replace: true });

  const machines = payload?.snapshot.machines;
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (machines ?? []).filter((m) => (status === 'all' || m.status === status) && (!hideTest || !m.is_test_machine)
      && (!needle || m.machine_id.toLowerCase().includes(needle) || (m.display_name ?? '').toLowerCase().includes(needle)))
      .sort((a, b) => SORTERS[sort](a, b) * dir);
  }, [machines, query, status, hideTest, sort, dir]);
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visible = filtered.slice((Math.min(page, pages) - 1) * PAGE_SIZE, Math.min(page, pages) * PAGE_SIZE);
  const running = Boolean(payload?.scan_state.running);
  const counts = useMemo(() => {
    const list = machines ?? [];
    return {
      complete: list.filter((m) => m.status === 'backfilled').length,
      needs: list.filter((m) => m.status === 'needs_backfill').length,
      noSource: list.filter((m) => m.status === 'no_source_data').length,
      errors: list.filter((m) => m.status === 'scan_error').length,
      test: list.filter((m) => m.is_test_machine).length,
    };
  }, [machines]);
  const quickMachine = quick ? machines?.find((m) => m.machine_id === quick) : undefined;

  const header = (key: SortKey, label: string) => <th scope="col" aria-sort={sort === key ? (dir === 1 ? 'ascending' : 'descending') : 'none'}>
    <button type="button" onClick={() => update({ sort: key, dir: sort === key && dir === 1 ? 'desc' : null })}>{label}{sort === key ? (dir === 1 ? ' ▲' : ' ▼') : ''}</button></th>;

  if (!payload && error) return <ErrorState message={error} onRetry={() => void reload()} />;

  return <>
    <div className="page-head">
      <div><h1>Fleet coverage</h1><ScanFreshness payload={payload} /></div>
      <div className="toolbar">
        <RefreshButton onClick={() => void reload()} fetching={refreshing || loading} label="Refresh" />
        <button type="button" className="btn btn-primary" onClick={() => void startScan()} disabled={running || scanRequesting || !payload}>
          {running || scanRequesting ? <Spinner /> : <Play size={14} />}{running ? 'Scanning…' : scanRequesting ? 'Starting…' : 'Rescan'}</button>
        {running ? <button type="button" className="btn" onClick={() => void resetScan()} title="Clear a stuck scan state"><RotateCcw size={14} />Reset scan</button> : null}
      </div>
    </div>
    {payload && running ? <ScanProgress payload={payload} /> : null}
    {payload && payload.snapshot.warnings?.length ? <details><summary>{payload.snapshot.warnings.length} scan warning(s)</summary>
      <ul>{payload.snapshot.warnings.map((warning) => <li key={warning} className="wrap">{warning}</li>)}</ul></details> : null}
    <div className="kpis" aria-label="Machine counts">
      <KpiChip label="Complete" value={counts.complete} tone="success" />
      <KpiChip label="Needs backfill" value={counts.needs} tone="warn" />
      <KpiChip label="No source" value={counts.noSource} />
      <KpiChip label="Scan errors" value={counts.errors} tone={counts.errors ? 'failed' : undefined} />
      <KpiChip label="Test" value={counts.test} />
    </div>
    <div className="toolbar">
      <input type="search" placeholder="Search id / name…" aria-label="Search machines" value={query} onChange={(e) => update({ q: e.target.value })} />
      <select aria-label="Status filter" value={status} onChange={(e) => update({ status: e.target.value === 'all' ? null : e.target.value })}>
        {STATUS_OPTIONS.map((option) => <option key={option} value={option}>{option === 'all' ? 'All statuses' : BACKFILL_STATUS[option].label}</option>)}
      </select>
      <label className="check"><input type="checkbox" checked={hideTest} onChange={(e) => update({ hide_test: e.target.checked ? '1' : null })} />Hide test</label>
      <span className="grow" />
      <span className="muted">{filtered.length} of {machines?.length ?? 0}</span>
      <a className="btn" href={`${API_BASE}/api/backfill/export/manifest?status=needs_backfill`}><Download size={14} />Export needs_backfill</a>
    </div>
    <div className={`table-wrap${refreshing ? ' stale' : ''}`}><table>
      <thead><tr>{header('machine', 'Machine')}{header('status', 'Status')}{header('coverage', 'Coverage')}{header('first_missing', 'First missing')}{header('last_populated', 'Last populated')}<th scope="col">Src</th></tr></thead>
      <tbody>
        {loading || (running && !machines?.length) ? <SkeletonRows columns={6} /> : visible.map((m) => (
          <tr key={m.machine_id} className="clickable" tabIndex={0} onClick={() => update({ quick: m.machine_id, page: params.get('page') })}
            onKeyDown={(e) => { if (e.key === 'Enter') update({ quick: m.machine_id, page: params.get('page') }); }}>
            <td><MachineTitle machine={m} /></td>
            <td><MachineStatusBadge machine={m} /></td>
            <td className="mono">{m.months_complete}/{m.months_expected}</td>
            <td className="mono">{m.first_missing_month ?? '—'}</td>
            <td className="mono">{m.last_populated_month ?? '—'}</td>
            <td className="mono" title={m.lifecycle_source ? `Lifecycle: ${m.lifecycle_source}` : 'No lifecycle enrichment'}>
              {payload?.snapshot.features_source === 'databricks' ? 'DBX' : 'Blob'}{m.crosscheck?.status === 'mismatch' ? ' ⚠' : m.crosscheck?.status === 'match' ? ' ✓' : ''}</td>
          </tr>))}
      </tbody>
    </table></div>
    {!loading && payload && !running && !visible.length ? (machines?.length
      ? <EmptyState title="No machines match" action={<button type="button" className="btn" onClick={() => setParams({}, { replace: true })}>Clear filters</button>}>Change the search or the status filter.</EmptyState>
      : <EmptyState title="No scan results" action={<button type="button" className="btn btn-primary" onClick={() => void startScan()}>Run a scan</button>}>The dashboard has no saved scan. A scan reads coverage for every machine.</EmptyState>) : null}
    {pages > 1 ? <nav className="pager" aria-label="Pages">
      <button type="button" className="btn" disabled={page <= 1} onClick={() => update({ page: String(page - 1) })}>‹ Prev</button>
      <span className="mono">{Math.min(page, pages)} / {pages}</span>
      <button type="button" className="btn" disabled={page >= pages} onClick={() => update({ page: String(page + 1) })}>Next ›</button>
    </nav> : null}
    {quick ? <>
      <div className="sheet-backdrop" onClick={() => update({ quick: null, page: params.get('page') })} />
      <aside className="sheet" role="dialog" aria-modal="true" aria-label="Machine quick view"
        onKeyDown={(e) => { if (e.key === 'Escape') update({ quick: null, page: params.get('page') }); }}>
        <div className="panel-head"><h2>{quickMachine ? <MachineTitle machine={quickMachine} /> : quick}</h2>
          <button type="button" className="icon-btn" autoFocus aria-label="Close quick view" onClick={() => update({ quick: null, page: params.get('page') })}><X size={16} /></button></div>
        {quickMachine ? <>
          <MachineFacts machine={quickMachine} />
          <MonthGrid months={quickMachine.months} />
          <Link className="btn btn-primary" to={`/monitor/machines/${encodeURIComponent(quickMachine.machine_id)}`}>Open full page</Link>
        </> : <EmptyState title="Machine not in the current scan">It may have been removed from the inventory.</EmptyState>}
      </aside>
    </> : null}
  </>;
}
