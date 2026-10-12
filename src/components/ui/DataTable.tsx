import { ChevronDown, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, ChevronUp } from 'lucide-react';
import { useMemo, type ReactNode } from 'react';
import { SkeletonRows } from './index';
import { PAGE_SIZES, useTableState } from './tableState';

export interface Column<T> {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  sort?: (a: T, b: T) => number;
  className?: string;
  /** Hide on narrow screens (<768px). */
  optional?: boolean;
  sticky?: boolean;
}

function pageNumbers(page: number, pages: number): (number | '…')[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const out: (number | '…')[] = [1];
  const from = Math.max(2, page - 1), to = Math.min(pages - 1, page + 1);
  if (from > 2) out.push('…');
  for (let i = from; i <= to; i += 1) out.push(i);
  if (to < pages - 1) out.push('…');
  out.push(pages);
  return out;
}

/**
 * Paginated, sortable table: sticky header, optional sticky first column, page size
 * 10/25/50/100, "Showing a–b of N", first/prev/numbers/next/last. State lives in the URL.
 */
export function DataTable<T>({ rows, columns, rowKey, prefix = '', defaultSize = 25, defaultSort = '', loading, empty, toolbar, overlay, onRowClick, rowClassName, compact, label, total }: {
  rows: T[]; columns: Column<T>[]; rowKey: (row: T) => string; prefix?: string; defaultSize?: number; defaultSort?: string;
  loading?: boolean; empty?: ReactNode; toolbar?: ReactNode; overlay?: ReactNode; onRowClick?: (row: T) => void;
  rowClassName?: (row: T) => string | undefined; compact?: boolean; label: string; total?: number;
}) {
  const state = useTableState(prefix, defaultSize, defaultSort);
  const sorted = useMemo(() => {
    const column = columns.find((c) => c.key === state.sort);
    return column?.sort ? [...rows].sort((a, b) => column.sort!(a, b) * state.dir) : rows;
  }, [rows, columns, state.sort, state.dir]);
  const pages = Math.max(1, Math.ceil(sorted.length / state.size));
  const page = Math.min(state.page, pages);
  const start = (page - 1) * state.size;
  const visible = sorted.slice(start, start + state.size);
  const showEmpty = !loading && !rows.length;
  return <div className="table-shell">
    {toolbar ? <div className="table-toolbar">{toolbar}</div> : null}
    {overlay}
    <div className="table-scroll" style={{ marginTop: toolbar ? 12 : 0 }}>
      <table className={compact ? 'compact' : undefined} aria-label={label}>
        <thead><tr>{columns.map((c) => <th key={c.key} scope="col" className={`${c.sticky ? 'sticky-col ' : ''}${c.optional ? 'hide-sm ' : ''}${c.className ?? ''}`}
          aria-sort={c.sort ? (state.sort === c.key ? (state.dir === 1 ? 'ascending' : 'descending') : 'none') : undefined}>
          {c.sort ? <button type="button" onClick={() => state.toggleSort(c.key)}>{c.header}
            {state.sort === c.key ? (state.dir === 1 ? <ChevronUp size={13} /> : <ChevronDown size={13} />) : null}</button> : c.header}</th>)}</tr></thead>
        <tbody>
          {loading && !rows.length ? <SkeletonRows columns={columns.length} rows={Math.min(state.size, 8)} /> : visible.map((row) => <tr key={rowKey(row)}
            className={[onRowClick ? 'clickable' : '', rowClassName?.(row) ?? ''].join(' ').trim() || undefined}
            tabIndex={onRowClick ? 0 : undefined} onClick={onRowClick ? () => onRowClick(row) : undefined}
            onKeyDown={onRowClick ? (e) => { if (e.key === 'Enter') onRowClick(row); } : undefined}>
            {columns.map((c) => <td key={c.key} className={`${c.sticky ? 'sticky-col ' : ''}${c.optional ? 'hide-sm ' : ''}${c.className ?? ''}`}>{c.cell(row)}</td>)}
          </tr>)}
        </tbody>
      </table>
      {showEmpty ? empty : null}
    </div>
    {rows.length ? <div className="table-foot">
      <span className="num">Showing {start + 1}–{Math.min(start + state.size, sorted.length)} of {sorted.length}{total !== undefined && total !== sorted.length ? ` (filtered from ${total})` : ''}</span>
      <label className="check">Rows per page
        <select value={state.size} onChange={(e) => state.setSize(Number(e.target.value))} aria-label="Rows per page" style={{ height: 32 }}>
          {PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}</select></label>
      {pages > 1 ? <nav className="pager" aria-label={`${label} pages`}>
        <button type="button" aria-label="First page" disabled={page <= 1} onClick={() => state.setPage(1)}><ChevronsLeft size={14} /></button>
        <button type="button" aria-label="Previous page" disabled={page <= 1} onClick={() => state.setPage(page - 1)}><ChevronLeft size={14} /></button>
        {pageNumbers(page, pages).map((n, i) => n === '…' ? <span key={`gap${i}`} className="muted">…</span>
          : <button key={n} type="button" aria-current={n === page ? 'page' : undefined} onClick={() => state.setPage(n)}>{n}</button>)}
        <button type="button" aria-label="Next page" disabled={page >= pages} onClick={() => state.setPage(page + 1)}><ChevronRight size={14} /></button>
        <button type="button" aria-label="Last page" disabled={page >= pages} onClick={() => state.setPage(pages)}><ChevronsRight size={14} /></button>
      </nav> : null}
    </div> : null}
  </div>;
}
