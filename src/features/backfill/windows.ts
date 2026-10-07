import type { SplitSpec } from './types';
import { orchestratorMonthIndex } from '../../utils';

/** Inclusive calendar end day → exclusive until bound at 00:00Z the next day. */
export function inclusiveEndDateToExclusiveUntil(day: string): string {
  const [y, m, d] = day.split('-').map(Number);
  const next = new Date(Date.UTC(y, m - 1, d + 1));
  const yy = next.getUTCFullYear();
  const mm = String(next.getUTCMonth() + 1).padStart(2, '0');
  const dd = String(next.getUTCDate()).padStart(2, '0');
  return `${yy}-${mm}-${dd}T00:00:00Z`;
}

export function startDateToSince(day: string): string {
  return `${day}T00:00:00Z`;
}

/** UTC month indices intersecting [sinceIso, untilExclusiveIso). */
export function monthIndicesInRange(sinceIso: string, untilExclusiveIso: string): number[] {
  if (!sinceIso || !untilExclusiveIso) return [];
  const start = Date.parse(sinceIso);
  const end = Date.parse(untilExclusiveIso);
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return [];
  const indices: number[] = [];
  const cursor = new Date(Date.UTC(new Date(start).getUTCFullYear(), new Date(start).getUTCMonth(), 1));
  while (cursor.getTime() < end) {
    const year = cursor.getUTCFullYear();
    const month = cursor.getUTCMonth() + 1;
    const next = new Date(Date.UTC(year, month, 1));
    if (cursor.getTime() < end && next.getTime() > start) {
      indices.push(orchestratorMonthIndex({ year, month }));
    }
    cursor.setUTCMonth(cursor.getUTCMonth() + 1);
  }
  return indices;
}

export interface PreviewWindow {
  monthIndex: number;
  since: Date;
  until: Date;
}

/** Preview-only mirror of backend split_windows; backend stays authoritative. */
export function previewWindows(
  sinceIso: string,
  untilIso: string,
  split: SplitSpec,
  now = new Date(),
): PreviewWindow[] {
  const start = Date.parse(sinceIso);
  let end = Date.parse(untilIso);
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return [];
  const nowCeil = new Date(now);
  nowCeil.setUTCMinutes(0, 0, 0);
  if (now.getUTCMinutes() || now.getUTCSeconds() || now.getUTCMilliseconds()) {
    nowCeil.setUTCHours(nowCeil.getUTCHours() + 1);
  }
  if (end > nowCeil.getTime()) end = nowCeil.getTime();
  const chunkDays = split.mode === 'month' ? null : split.mode === 'day' ? 1 : split.mode === 'week' ? 7 : Math.max(1, Math.min(31, Number(split.days) || 1));
  const out: PreviewWindow[] = [];
  let cursor = new Date(Date.UTC(new Date(start).getUTCFullYear(), new Date(start).getUTCMonth(), 1));
  while (cursor.getTime() < end) {
    const year = cursor.getUTCFullYear();
    const month = cursor.getUTCMonth() + 1;
    const nextMonth = new Date(Date.UTC(year, month, 1));
    const monthStart = Math.max(start, cursor.getTime());
    const monthEnd = Math.min(end, nextMonth.getTime());
    if (monthStart < monthEnd) {
      const monthIndex = orchestratorMonthIndex({ year, month });
      if (chunkDays === null) {
        out.push({ monthIndex, since: new Date(monthStart), until: new Date(monthEnd) });
      } else {
        let chunkStart = monthStart;
        while (chunkStart < monthEnd) {
          const chunkEnd = Math.min(chunkStart + chunkDays * 86_400_000, monthEnd);
          out.push({ monthIndex, since: new Date(chunkStart), until: new Date(chunkEnd) });
          chunkStart = chunkEnd;
        }
      }
    }
    cursor = nextMonth;
  }
  return out;
}

export function previewSummary(sinceIso: string, untilIso: string, split: SplitSpec): string {
  const windows = previewWindows(sinceIso, untilIso, split);
  if (!windows.length) return 'No manifests planned yet.';
  const months = new Set(windows.map((w) => w.monthIndex)).size;
  const largestDays = Math.max(...windows.map((w) => (w.until.getTime() - w.since.getTime()) / 86_400_000));
  const splitNote = split.mode === 'month'
    ? ''
    : ' Per-day/week/N-day split needs the orchestrator update (contract v5) before a live trigger.';
  return `${windows.length} manifest${windows.length === 1 ? '' : 's'} across ${months} month${months === 1 ? '' : 's'}; largest window ${largestDays} day${largestDays === 1 ? '' : 's'}.${splitNote}`;
}
