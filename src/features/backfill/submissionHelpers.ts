import type { AdminAction } from '../../types';

export function isTriggerPreparationActive(action: AdminAction | null | undefined) {
  return Boolean(action && ['queued', 'cancel_window', 'validating', 'preparing_manifests', 'submitting'].includes(action.phase ?? ''));
}
