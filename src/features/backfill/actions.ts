import { postJson } from '../../lib/api';
import type { AdminAction } from '../../types';

export function canCancelBeforeSubmit(action: AdminAction | null | undefined) {
  return Boolean(action && action.status === 'queued' && ['queued', 'cancel_window', 'validating', 'preparing_manifests'].includes(action.phase ?? ''));
}

export async function cancelBeforeSubmit(actionId: string): Promise<AdminAction> {
  const result = await postJson<{ action?: AdminAction }>(`/api/admin/actions/${encodeURIComponent(actionId)}/cancel-before-submit`);
  if (!result.action) throw new Error('Cancellation returned no action record.');
  return result.action;
}

export function phaseLabel(phase?: string) {
  const labels: Record<string, string> = {
    queued: 'Queued', cancel_window: 'Queued (cancel window)', validating: 'Validating', preparing_manifests: 'Preparing manifests',
    submitting: 'Dispatching', accepted: 'Submitted', failed: 'Failed', cancelled: 'Cancelled',
  };
  return phase ? labels[phase] ?? phase.replaceAll('_', ' ') : 'Preparing';
}
