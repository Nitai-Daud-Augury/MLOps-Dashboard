import type { AdminAction } from '../../types';
import { API_BASE } from '../../constants';
import { isTriggerPreparationActive } from './submissionHelpers';

/** Initial delay between admin-action status polls (ms). */
export const ADMIN_ACTION_POLL_INITIAL_MS = 3_000;
/** After ~60s still pending, step up to this delay. */
export const ADMIN_ACTION_POLL_MID_MS = 5_000;
/** Cap after ~3 minutes still pending. */
export const ADMIN_ACTION_POLL_MAX_MS = 10_000;

export function isAdminActionTerminal(action: AdminAction | null | undefined): boolean {
  if (!action) return false;
  return (
    ['accepted', 'failed', 'cancelled'].includes(action.phase ?? '')
    || ['failed', 'succeeded', 'cancelled'].includes(action.status)
  );
}

/** Light backoff while an action stays in a non-terminal preparation phase. */
export function nextAdminActionPollDelayMs(elapsedMs: number): number {
  if (elapsedMs >= 180_000) return ADMIN_ACTION_POLL_MAX_MS;
  if (elapsedMs >= 60_000) return ADMIN_ACTION_POLL_MID_MS;
  return ADMIN_ACTION_POLL_INITIAL_MS;
}

export type AdminActionPollHandlers = {
  onUpdate: (action: AdminAction) => void;
  onAccepted?: (action: AdminAction) => void | Promise<void>;
  onFailed?: (action: AdminAction) => void | Promise<void>;
  onTerminal?: (action: AdminAction) => void | Promise<void>;
};

/**
 * Single-flight poller for GET /api/admin/actions/:id.
 * Waits for each response before scheduling the next tick; clears on unmount
 * and when the action reaches a terminal phase/status.
 */
export function startAdminActionPoll(
  actionId: string,
  handlers: AdminActionPollHandlers,
): () => void {
  let cancelled = false;
  let timer: number | undefined;
  let inFlight = false;
  const startedAt = Date.now();

  const schedule = (delayMs: number) => {
    if (cancelled) return;
    timer = window.setTimeout(() => {
      void tick();
    }, delayMs);
  };

  const tick = async () => {
    if (cancelled || inFlight) return;
    inFlight = true;
    try {
      const response = await fetch(
        `${API_BASE}/api/admin/actions/${encodeURIComponent(actionId)}`,
        { cache: 'no-store' },
      );
      const payload = await response.json() as { action?: AdminAction; detail?: string };
      if (!response.ok || !payload.action) {
        throw new Error(payload.detail ?? 'Could not refresh admin action status.');
      }
      if (cancelled) return;
      const action = payload.action;
      handlers.onUpdate(action);
      if (action.phase === 'accepted') {
        await handlers.onAccepted?.(action);
        await handlers.onTerminal?.(action);
        return;
      }
      if (action.phase === 'failed' || action.status === 'failed') {
        await handlers.onFailed?.(action);
        await handlers.onTerminal?.(action);
        return;
      }
      if (isAdminActionTerminal(action) || !isTriggerPreparationActive(action)) {
        await handlers.onTerminal?.(action);
        return;
      }
    } catch {
      // Keep polling through transient status-read failures.
    } finally {
      inFlight = false;
    }
    if (!cancelled) {
      schedule(nextAdminActionPollDelayMs(Date.now() - startedAt));
    }
  };

  void tick();

  return () => {
    cancelled = true;
    if (timer !== undefined) window.clearTimeout(timer);
  };
}
