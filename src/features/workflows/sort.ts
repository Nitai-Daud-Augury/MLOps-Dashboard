import type { RunningWorkflow } from '../../types';

/** Pending dispatch rows first, then newest. */
export function workflowSort(a: RunningWorkflow, b: RunningWorkflow) {
  const time = (w: RunningWorkflow) => Date.parse(w.finished_at ?? w.started_at ?? w.created_at ?? '') || 0;
  return Number(Boolean(b.pending)) - Number(Boolean(a.pending)) || time(b) - time(a);
}

/** Keep the given order, but move each linked child directly under its parent (when listed). */
export function nestChildren(workflows: RunningWorkflow[]) {
  const ids = new Set(workflows.map((w) => w.workflow_id));
  const children = new Map<string, RunningWorkflow[]>();
  for (const w of workflows) {
    if (w.parent_workflow_id && ids.has(w.parent_workflow_id)) children.set(w.parent_workflow_id, [...(children.get(w.parent_workflow_id) ?? []), w]);
  }
  const out: RunningWorkflow[] = [];
  for (const w of workflows) {
    if (w.parent_workflow_id && ids.has(w.parent_workflow_id)) continue;
    out.push(w, ...(children.get(w.workflow_id) ?? []));
  }
  return out;
}
