import type { RunningWorkflow } from '../../types';

const DAY_MS = 24 * 3600_000;

/**
 * workflows/summary.counts are not time-windowed, so the "(24h)" KPIs are computed here from
 * finished_at (falling back to started_at, then created_at).
 */
export function finishedWithin(workflows: RunningWorkflow[], statuses: string[], windowMs = DAY_MS, now = Date.now()): RunningWorkflow[] {
  return workflows.filter((w) => {
    if (!statuses.includes(w.status.toLowerCase())) return false;
    const stamp = Date.parse(w.finished_at ?? w.started_at ?? w.created_at ?? '');
    return Number.isFinite(stamp) && now - stamp <= windowMs;
  });
}

export const FAILED = ['failed', 'error'];
export const DONE = ['succeeded', 'completed'];
