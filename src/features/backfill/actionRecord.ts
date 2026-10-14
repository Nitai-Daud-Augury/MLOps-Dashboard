import type { AdminAction } from '../../types';

/** Machines an action touched: machine_ids, else the manifest_windows keys (FST backfill rows). */
export function actionMachines(a: AdminAction): string[] {
  if (a.machine_ids?.length) return a.machine_ids;
  return a.manifest_windows ? Object.keys(a.manifest_windows) : [];
}

/** Short run label from an Argo/Outerbounds workflow id: last dotted part, without the flow-name prefix. */
export function shortRunId(id?: string | null): string {
  if (!id) return '';
  const last = id.split('.').pop() ?? id;
  const parts = last.split('-');
  return parts.length > 1 ? parts.slice(1).join('-') : last;
}

/** Error text to show: error, else the last stderr line of a failed action. */
export function actionError(a: AdminAction): string {
  if (a.error) return a.error;
  if (a.status !== 'failed' && a.phase !== 'failed') return '';
  return (a.stderr ?? '').trim().split('\n').pop() ?? '';
}
