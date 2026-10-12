import type { RunningWorkflow } from '../../types';

/** Pending dispatch rows first, then newest. */
export function workflowSort(a: RunningWorkflow, b: RunningWorkflow) {
  const time = (w: RunningWorkflow) => Date.parse(w.finished_at ?? w.started_at ?? w.created_at ?? '') || 0;
  return Number(Boolean(b.pending)) - Number(Boolean(a.pending)) || time(b) - time(a);
}
