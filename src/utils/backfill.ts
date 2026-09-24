import { ORCHESTRATOR_FIRST_MONTH } from '../constants';
import type { MonthPartition } from '../types';

export function orchestratorMonthIndex({ year, month }: Pick<MonthPartition, 'year' | 'month'>) {
  return (year - ORCHESTRATOR_FIRST_MONTH.year) * 12 + month - ORCHESTRATOR_FIRST_MONTH.month;
}

export function orchestratorMonthsThroughToday(now = new Date()) {
  const lastIndex = orchestratorMonthIndex({
    year: now.getUTCFullYear(),
    month: now.getUTCMonth() + 1,
  });
  return Array.from({ length: lastIndex + 1 }, (_, index) => {
    const offset = ORCHESTRATOR_FIRST_MONTH.month - 1 + index;
    return {
      index,
      year: ORCHESTRATOR_FIRST_MONTH.year + Math.floor(offset / 12),
      month: offset % 12 + 1,
    };
  });
}

export function utcToday(now = new Date()) {
  return now.toISOString().slice(0, 10);
}

export function backfillMonthUntil(year: number, month: number, now = new Date()) {
  const nextYear = month === 12 ? year + 1 : year;
  const nextMonth = month === 12 ? 1 : month + 1;
  const nextMonthStart = `${nextYear}-${String(nextMonth).padStart(2, '0')}-01T00:00:00`;
  const current = { year: now.getUTCFullYear(), month: now.getUTCMonth() + 1 };
  return year === current.year && month === current.month ? now.toISOString() : nextMonthStart;
}
