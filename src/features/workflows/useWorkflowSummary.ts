import { useCallback, useState } from 'react';
import { errorText, useResource } from '../../lib/api';
import type { WorkflowSummaryPayload } from '../../types';
import { writeCachedJson } from '../../utils/cache';
import { postWorkflowRefresh, useRetryCountdown, useWorkflowLiveSocket } from './workflowStatusRefresh';

/**
 * Workflow summary (incl. pending dispatch rows): cached GET on open, explicit rate-limited
 * Refresh, and the push socket. Never polls in the background.
 */
export function useWorkflowSummary({ live = true, onLimited }: { live?: boolean; onLimited?: (seconds: number) => void } = {}) {
  const summary = useResource<WorkflowSummaryPayload>('/api/admin/workflows/summary', { cacheKey: 'admin-workflow-summary', maxAgeMs: 2 * 60_000 });
  const [retryIn, applyRetry] = useRetryCountdown();
  const [requesting, setRequesting] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [socketSeen, setSocketSeen] = useState(false);
  const { set } = summary;
  useWorkflowLiveSocket(live, (pushed) => {
    setSocketSeen(true);
    set({ ...(summary.data ?? {}), ...pushed });
    writeCachedJson('admin-workflow-summary', pushed);
  });
  const refresh = useCallback(async () => {
    setRequesting(true);
    setRefreshError(null);
    try {
      const result = await postWorkflowRefresh();
      set(result);
      applyRetry(result.refresh);
      if (result.refresh?.state === 'limited') onLimited?.(result.refresh.retry_after ?? 0);
    } catch (exc) {
      setRefreshError(errorText(exc));
    } finally { setRequesting(false); }
  }, [set, applyRetry, onLimited]);
  const data = summary.data;
  const fetching = requesting || Boolean(data?.refreshing || data?.refresh?.refreshing);
  return { ...summary, refresh, retryIn, fetching, refreshError, socketSeen };
}
