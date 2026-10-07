import { useCallback, useEffect, useRef, useState } from 'react';
import type { AdminAction } from '../../types';
import { API_BASE } from '../../constants';
import { isTriggerPreparationActive } from './submissionHelpers';
import { startAdminActionPoll } from './adminActionPoll';

export function useOrchestratedBackfillSubmission({
  onAccepted,
  onFailed,
  onToast,
}: {
  onAccepted?: (action: AdminAction) => void | Promise<void>;
  onFailed?: (action: AdminAction) => void | Promise<void>;
  onToast?: (type: 'success' | 'error' | 'info', message: string) => void;
}) {
  const [submissionAction, setSubmissionAction] = useState<AdminAction | null>(null);
  const submissionActive = isTriggerPreparationActive(submissionAction);
  const pollActionId = submissionActive ? submissionAction?.id ?? null : null;

  // Keep latest callbacks in refs so the poll effect only depends on action id
  // (unstable inline lambdas must not restart the poller every render).
  const onAcceptedRef = useRef(onAccepted);
  const onFailedRef = useRef(onFailed);
  const onToastRef = useRef(onToast);
  onAcceptedRef.current = onAccepted;
  onFailedRef.current = onFailed;
  onToastRef.current = onToast;

  useEffect(() => {
    if (!pollActionId) return;
    return startAdminActionPoll(pollActionId, {
      onUpdate: (action) => setSubmissionAction(action),
      onAccepted: async (action) => {
        onToastRef.current?.('success', 'Backfill accepted.');
        await onAcceptedRef.current?.(action);
      },
      onFailed: async (action) => {
        const message = action.error ?? 'Backfill submission failed.';
        onToastRef.current?.('error', message);
        await onFailedRef.current?.(action);
      },
    });
  }, [pollActionId]);

  const submit = useCallback(async (body: Record<string, unknown>, queuedMessage?: string) => {
    const response = await fetch(`${API_BASE}/api/admin/backfills/orchestrated`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const result = await response.json() as { action?: AdminAction; detail?: string };
    if (!response.ok) throw new Error(result.detail ?? `Request failed: ${response.status}`);
    if (!result.action?.id) throw new Error('The backfill request was accepted without an action ID.');
    setSubmissionAction(result.action);
    if (queuedMessage) onToastRef.current?.('info', queuedMessage);
    return result.action;
  }, []);

  return { submissionAction, setSubmissionAction, submissionActive, submit };
}
