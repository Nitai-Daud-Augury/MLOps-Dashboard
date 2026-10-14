import type { ActiveMonth, MonthStatus } from '../../types';
import { orchestratorMonthIndex } from '../../utils';
import { BACKFILL_STATUS, TONE_ICON, type Tone } from '../../lib/status';

const SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function cellTone(month: MonthStatus, active?: ActiveMonth): { tone: Tone; label: string } {
  if (active?.state === 'running' || active?.state === 'stopping' || active?.state === 'cancel_requested') return { tone: 'running', label: 'running' };
  if (active?.state === 'queued' || active?.state === 'requeue_pending') return { tone: 'queued', label: 'queued' };
  if (month.activity_status === 'not_installed') return { tone: 'na', label: 'pre-install' };
  const crossFlag = month.crosscheck && (month.crosscheck.status === 'mismatch' || month.crosscheck.status === 'feature_store_stale');
  const base = BACKFILL_STATUS[month.status] ?? BACKFILL_STATUS.unknown;
  if (month.status === 'backfilled' && crossFlag) return { tone: 'warn', label: month.crosscheck?.status === 'mismatch' ? 'crosscheck mismatch' : 'Databricks copy stale' };
  return { tone: month.status === 'needs_backfill' ? 'failed' : base.tone, label: month.status === 'needs_backfill' ? 'missing' : base.label.toLowerCase() };
}

/** Text month grid (no chart): one row per year, a cell per month with icon + tooltip. */
export function MonthGrid({ months: raw, active }: { months?: MonthStatus[] | null; active?: ActiveMonth[] | null }) {
  // Real scans can omit months or a partition; skip those instead of crashing the page.
  const months = (raw ?? []).filter((m) => m && m.partition);
  if (!months.length) return <p className="muted">No month partitions in the last scan.</p>;
  const activeByIndex = new Map((active ?? []).map((item) => [item.month_index, item]));
  const years = new Map<number, MonthStatus[]>();
  for (const month of [...months].sort((a, b) => a.partition.index - b.partition.index)) {
    (years.get(month.partition.year) ?? years.set(month.partition.year, []).get(month.partition.year)!).push(month);
  }
  return <div className="month-grid">
    {[...years.entries()].map(([year, list]) => <div className="month-year" key={year}>
      <strong>{year}</strong>
      {list.map((month) => {
        const { tone, label } = cellTone(month, activeByIndex.get(orchestratorMonthIndex(month.partition)));
        const rows = month.row_count !== undefined ? ` · ${month.row_count} rows` : '';
        return <span key={month.partition.index} className={`month-cell tone-${tone}`} title={`${month.partition.label}: ${label}${rows}${month.reason ? ` · ${month.reason}` : ''}`}>
          {SHORT[month.partition.month - 1]} {TONE_ICON[tone]}
        </span>;
      })}
    </div>)}
    <div className="legend" aria-label="Legend">
      <span>✓ complete</span><span>✗ missing</span><span>⧗ running</span><span>◌ queued</span><span>— n/a / pre-install</span><span>⚠ crosscheck mismatch / stale</span>
    </div>
  </div>;
}
