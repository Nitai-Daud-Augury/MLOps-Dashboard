import { AlertOctagon, CheckCircle2, CircleSlash, Download, ExternalLink, Play, RotateCcw, Rows3, Search, Server, TriangleAlert, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { API_BASE, STATUS_OPTIONS } from '../../constants';
import { EmptyState, ErrorState, RefreshButton, Spinner, Timestamp } from '../../components/ui';
import { DataTable, type Column } from '../../components/ui/DataTable';
import { KpiCard, Meter, PageHeader, Section, type KpiState } from '../../components/ui/Layout';
import { MachineFacts, MachineStatusBadge, MachineTitle } from '../../features/machines/MachineSummary';
import { MonthGrid } from '../../features/machines/MonthGrid';
import { duration, exactTime } from '../../lib/format';
import { BACKFILL_STATUS } from '../../lib/status';
import { useStatus } from '../../state/statusStore';
import type { BackfillStatus, MachineStatus, StatusPayload } from '../../types';

/** "Last scanned" slot: restore state after an app restart, else the scan time. */
export function ScanFreshness({ payload }: { payload: StatusPayload | null }) {
  if (!payload) return <span className="muted"><Spinner /> Loading status…</span>;
  const persistence = payload.persistence;
  if (persistence?.loading) return <span className="muted" role="status"><Spinner /> Loading saved scan…</span>;
  const generated = payload.snapshot.machines.length ? payload.snapshot.generated_at : null;
  if (!generated) return <span className="muted">No scan yet — run a scan to compute coverage.</span>;
  return <span>Last scanned <span className="mono" title="Asia/Jerusalem">{exactTime(generated)}</span> (<Timestamp value={generated} />)
    {persistence?.loaded_from ? <> <span className="badge tone-na" title="These results were restored from the saved scan after an app restart. Rescan to refresh them.">restored after restart</span></> : null}
    {payload.snapshot.features_source ? <span className="muted"> · from {payload.snapshot.features_source}</span> : null}
  </span>;
}

function ScanProgress({ payload }: { payload: StatusPayload }) {
  const state = payload.scan_state;
  const total = state.total_machines || 0;
  const pct = total ? Math.round((state.completed_machines / total) * 100) : 0;
  return <div className="card section progress" role="status" aria-live="polite">
    <span><Spinner /> Scanning {total ? `${pct}% · ${state.completed_machines}/${total} machines` : `(${state.phase ?? 'starting'})`} · {duration(state.started_at)} elapsed</span>
    <div className="progress-track" aria-hidden="true"><div className="progress-fill" style={{ width: `${total ? pct : 5}%` }} /></div>
  </div>;
}

const coverage = (m: MachineStatus) => m.months_complete / (m.months_expected || 1);
const COLUMNS: Column<MachineStatus>[] = [
  { key: 'machine', header: 'Machine', sticky: true, cell: (m) => <div className="cell-stack"><MachineTitle machine={m} />{m.display_name && m.display_name !== m.machine_id ? <span className="mono">{m.machine_id}</span> : null}</div>,
    sort: (a, b) => (a.display_name || a.machine_id).localeCompare(b.display_name || b.machine_id) },
  { key: 'status', header: 'Status', cell: (m) => <MachineStatusBadge machine={m} />, sort: (a, b) => a.status.localeCompare(b.status) },
  { key: 'coverage', header: 'Coverage', cell: (m) => <Meter done={m.months_complete} total={m.months_expected} />, sort: (a, b) => coverage(a) - coverage(b) },
  { key: 'first_missing', header: 'First missing', optional: true, cell: (m) => <span className="mono">{m.first_missing_month ?? '—'}</span>,
    sort: (a, b) => (a.first_missing_month ?? '9999').localeCompare(b.first_missing_month ?? '9999') },
  { key: 'last_populated', header: 'Last populated', optional: true, cell: (m) => <span className="mono">{m.last_populated_month ?? '—'}</span>,
    sort: (a, b) => (a.last_populated_month ?? '').localeCompare(b.last_populated_month ?? '') },
  { key: 'check', header: 'Cross-check', optional: true, cell: (m) => <span className="muted">{m.crosscheck?.status === 'mismatch' ? '⚠ mismatch' : m.crosscheck?.status === 'match' ? '✓ match' : '—'}</span> },
  { key: 'open', header: <span className="sr-only">Open</span>, cell: (m) => <Link className="icon-btn" to={`/monitor/machines/${encodeURIComponent(m.machine_id)}`} aria-label={`Open ${m.machine_id}`} title="Open machine page" onClick={(e) => e.stopPropagation()}><ExternalLink size={14} /></Link> },
];

export function FleetPage() {
  const { payload, loading, error, refreshing, reload, startScan, resetScan, scanRequesting } = useStatus();
  const [params, setParams] = useSearchParams();
  const query = params.get('q') ?? '';
  const statuses = (params.get('status') ?? '').split(',').filter(Boolean) as BackfillStatus[];
  const hideTest = params.get('hide_test') === '1';
  const quick = params.get('quick');
  const [search, setSearch] = useState(query);
  const [compact, setCompact] = useState(() => localStorage.getItem('ulrpm-density') === 'compact');
  useEffect(() => { localStorage.setItem('ulrpm-density', compact ? 'compact' : 'comfortable'); }, [compact]);
  const update = (changes: Record<string, string | null>) => setParams((current) => {
    const next = new URLSearchParams(current);
    for (const [key, value] of Object.entries(changes)) { if (value === null || value === '') next.delete(key); else next.set(key, value); }
    if (!('quick' in changes)) next.delete('page');
    return next;
  }, { replace: true });
  // Debounced search (250ms) keeps typing smooth on large fleets.
  useEffect(() => { const id = window.setTimeout(() => { if (search !== query) update({ q: search }); }, 250); return () => window.clearTimeout(id); });

  const machines = payload?.snapshot.machines;
  const pool = useMemo(() => (machines ?? []).filter((m) => !hideTest || !m.is_test_machine), [machines, hideTest]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return pool.filter((m) => (!statuses.length || statuses.includes(m.status))
      && (!needle || m.machine_id.toLowerCase().includes(needle) || (m.display_name ?? '').toLowerCase().includes(needle)));
  }, [pool, query, statuses]);
  const count = (status: BackfillStatus) => pool.filter((m) => m.status === status).length;
  const running = Boolean(payload?.scan_state.running);
  const kpiState: KpiState = !payload ? 'loading' : running ? 'scanning' : error ? 'stale' : 'ready';
  const toggleStatus = (status: BackfillStatus, only = false) => {
    const next = only ? (statuses.length === 1 && statuses[0] === status ? [] : [status]) : statuses.includes(status) ? statuses.filter((s) => s !== status) : [...statuses, status];
    update({ status: next.join(',') || null });
  };
  const quickMachine = quick ? machines?.find((m) => m.machine_id === quick) : undefined;
  const filtersOn = Boolean(query || statuses.length || hideTest);
  const total = machines?.length ?? 0;
  const pct = (n: number) => (pool.length ? `${Math.round((n / pool.length) * 100)}% of fleet` : '—');

  if (!payload && error) return <ErrorState message={error} onRetry={() => void reload()} />;

  return <>
    <PageHeader title="Fleet coverage" purpose="Shows which machines have complete feature history and which need a backfill. Read-only."
      meta={<ScanFreshness payload={payload} />}
      actions={<>
        <RefreshButton onClick={() => void reload()} fetching={refreshing || loading} label="Refresh" />
        <button type="button" className="btn btn-primary" onClick={() => void startScan()} disabled={running || scanRequesting || !payload} title="Reads coverage for every machine from the active features source">
          {running || scanRequesting ? <Spinner /> : <Play size={14} />}{running ? 'Scanning…' : scanRequesting ? 'Starting…' : 'Rescan'}</button>
        {running ? <button type="button" className="btn" onClick={() => void resetScan()} title="Clear a stuck scan state"><RotateCcw size={14} />Reset scan</button> : null}
      </>} />
    {payload && running ? <ScanProgress payload={payload} /> : null}
    {payload && payload.snapshot.warnings?.length ? <details className="card section"><summary>{payload.snapshot.warnings.length} scan warning(s)</summary>
      <ul>{payload.snapshot.warnings.map((warning) => <li key={warning} className="wrap">{warning}</li>)}</ul></details> : null}
    <div className="kpis" aria-label="Machine counts">
      <KpiCard icon={<Server size={20} />} tone="primary" label="Machines" value={pool.length} state={kpiState} active={!statuses.length && filtersOn}
        helper={hideTest ? `${total - pool.length} test machines hidden` : `${pool.filter((m) => m.is_test_machine).length} test machines included`}
        tooltip="All machines in the last scan (respects the Hide test toggle)." onClick={() => update({ status: null })} />
      <KpiCard icon={<CheckCircle2 size={20} />} tone="ok" label="Complete" value={count('backfilled')} helper={pct(count('backfilled'))} state={kpiState}
        tooltip="Status backfilled: every expected month has features." active={statuses.length === 1 && statuses[0] === 'backfilled'} onClick={() => toggleStatus('backfilled', true)} />
      <KpiCard icon={<TriangleAlert size={20} />} tone="warn" label="Needs backfill" value={count('needs_backfill')} helper="Missing one or more months" state={kpiState}
        tooltip="Status needs_backfill: at least one expected month is missing. Start a backfill from Admin." active={statuses.length === 1 && statuses[0] === 'needs_backfill'} onClick={() => toggleStatus('needs_backfill', true)} />
      <KpiCard icon={<CircleSlash size={20} />} tone="neutral" label="No source data" value={count('no_source_data')} helper="Nothing to backfill from" state={kpiState}
        tooltip="Status no_source_data: the raw source has no data for this machine." active={statuses.length === 1 && statuses[0] === 'no_source_data'} onClick={() => toggleStatus('no_source_data', true)} />
      <KpiCard icon={<AlertOctagon size={20} />} tone={count('scan_error') ? 'danger' : 'neutral'} label="Scan errors" value={count('scan_error')} helper={count('scan_error') ? 'Rescan or check the reason' : 'None'} state={kpiState}
        tooltip="Status scan_error: the scan could not read this machine." active={statuses.length === 1 && statuses[0] === 'scan_error'} onClick={() => toggleStatus('scan_error', true)} />
    </div>
    <Section flush title="Machines" description="Every machine in the last scan with its month coverage. Click a row for a quick view; filter with the cards above or the controls below."
      actions={<a className="btn" href={`${API_BASE}/api/backfill/export/manifest?status=needs_backfill`} title="Download the needs_backfill machine list as a manifest"><Download size={14} />Export needs backfill</a>}>
      <DataTable label="Machines" rows={filtered} total={pool.length} columns={COLUMNS} rowKey={(m) => m.machine_id} defaultSort="status" compact={compact}
        loading={loading || (running && !machines?.length)}
        overlay={running && machines?.length ? <div className="scan-overlay"><Spinner /> A scan is running; these rows are from the previous scan.</div> : null}
        onRowClick={(m) => update({ quick: m.machine_id })}
        toolbar={<>
          <label className="search"><Search size={15} aria-hidden="true" /><input type="search" placeholder="Search ID or name…" aria-label="Search machines" value={search} onChange={(e) => setSearch(e.target.value)} /></label>
          <div className="seg-group" role="group" aria-label="Status filter">
            {STATUS_OPTIONS.filter((s) => s !== 'all').map((s) => <button key={s} type="button" className={`seg${statuses.includes(s as BackfillStatus) ? ' seg-on' : ''}`}
              aria-pressed={statuses.includes(s as BackfillStatus)} onClick={() => toggleStatus(s as BackfillStatus)}>
              {BACKFILL_STATUS[s as BackfillStatus].label} <span className="muted num">{count(s as BackfillStatus)}</span></button>)}
          </div>
          <label className="check"><input type="checkbox" checked={hideTest} onChange={(e) => update({ hide_test: e.target.checked ? '1' : null })} />Hide test</label>
          <span className="grow" />
          {filtersOn ? <button type="button" className="btn btn-ghost btn-sm" onClick={() => { setSearch(''); setParams({}, { replace: true }); }}><X size={14} />Clear all</button> : null}
          <button type="button" className="btn btn-sm" aria-pressed={compact} onClick={() => setCompact((v) => !v)} title="Toggle row density"><Rows3 size={14} />{compact ? 'Comfortable' : 'Compact'}</button>
        </>}
        empty={!payload ? null : machines?.length
          ? <EmptyState icon={<Search size={20} />} title="No machines match" action={<button type="button" className="btn" onClick={() => { setSearch(''); setParams({}, { replace: true }); }}>Clear filters</button>}>Change the search or the status filter.</EmptyState>
          : running ? <EmptyState icon={<Spinner size={20} />} title="Scan in progress">Results appear when the first scan finishes.</EmptyState>
          : <EmptyState icon={<Server size={20} />} title="No scan results" action={<button type="button" className="btn btn-primary" onClick={() => void startScan()}>Run a scan</button>}>The dashboard has no saved scan. A scan reads coverage for every machine.</EmptyState>} />
    </Section>
    {quick ? <>
      <div className="sheet-backdrop" onClick={() => update({ quick: null })} />
      <aside className="sheet" role="dialog" aria-modal="true" aria-label="Machine quick view" onKeyDown={(e) => { if (e.key === 'Escape') update({ quick: null }); }}>
        <div className="panel-head"><h2>{quickMachine ? <MachineTitle machine={quickMachine} /> : quick}</h2>
          <button type="button" className="icon-btn" autoFocus aria-label="Close quick view" onClick={() => update({ quick: null })}><X size={16} /></button></div>
        {quickMachine ? <>
          <MachineFacts machine={quickMachine} />
          <h3>Months</h3>
          <MonthGrid months={quickMachine.months} />
          <Link className="btn btn-primary" to={`/monitor/machines/${encodeURIComponent(quickMachine.machine_id)}`}>Open full page</Link>
        </> : <EmptyState title="Machine not in the current scan">It may have been removed from the inventory.</EmptyState>}
      </aside>
    </> : null}
  </>;
}
