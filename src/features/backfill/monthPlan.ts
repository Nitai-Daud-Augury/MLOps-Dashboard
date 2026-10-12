import type { MachineStatus, MonthStatus } from '../../types';
import { orchestratorMonthIndex, orchestratorMonthsThroughToday } from '../../utils';
import { monthIndicesInRange } from './windows';

export type MonthMode = 'gaps' | 'all' | 'range';

export function isMonthBackfillable(month: MonthStatus) {
  const activity = month.activity_status ?? (typeof month.row_count === 'number' && month.row_count > 0 ? 'online' : 'unknown');
  return activity === 'online' && (month.status === 'needs_backfill' || month.status === 'backfilled');
}

/**
 * Per-machine orchestrator month indices to backfill. gaps: needs_backfill months; all: every
 * backfillable month; range: backfillable months intersecting [since, until).
 */
export function buildMonthPlan(machines: MachineStatus[], machineIds: string[], mode: MonthMode, since: string, until: string): Record<string, number[]> {
  const supported = new Set(orchestratorMonthsThroughToday().map(({ index }) => index));
  const rangeSet = mode === 'range' ? new Set(monthIndicesInRange(since, until)) : null;
  const plan: Record<string, number[]> = {};
  for (const id of machineIds) {
    const machine = machines.find((item) => item.machine_id === id);
    if (!machine) continue;
    const indices = machine.months.filter((month) => {
      const index = orchestratorMonthIndex(month.partition);
      if (!supported.has(index) || !isMonthBackfillable(month)) return false;
      if (mode === 'gaps') return month.status === 'needs_backfill';
      if (mode === 'range') return Boolean(rangeSet?.has(index));
      return true;
    }).map((month) => orchestratorMonthIndex(month.partition));
    if (indices.length) plan[id] = [...new Set(indices)].sort((a, b) => a - b);
  }
  return plan;
}
