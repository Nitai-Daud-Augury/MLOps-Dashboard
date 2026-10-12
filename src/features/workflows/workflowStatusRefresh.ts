import { useCallback, useEffect, useRef, useState } from 'react';
import { API_BASE } from '../../constants';
import type { WorkflowRefreshInfo, WorkflowSummaryPayload } from '../../types';
import { workflowSocketUrl, writeCachedJson } from '../../utils/cache';

/**
 * Explicit Refresh: POST /api/admin/workflows/refresh. The backend starts or joins ONE
 * status refresh (rate limited per user and globally) and always answers with the cached
 * summary; `refresh.state === 'limited'` carries `retry_after` seconds.
 */
export async function postWorkflowRefresh(): Promise<WorkflowSummaryPayload> {
  const response = await fetch(`${API_BASE}/api/admin/workflows/refresh`, { method: 'POST', cache: 'no-store' });
  const payload = await response.json() as WorkflowSummaryPayload & { detail?: string };
  if (!response.ok) throw new Error(payload.detail ?? `Refresh failed: ${response.status}`);
  writeCachedJson('admin-workflow-summary', payload);
  return payload;
}

/** Seconds left before Refresh is allowed again (a local countdown; it never calls the API). */
export function useRetryCountdown(): [number, (info?: WorkflowRefreshInfo | null) => void] {
  const [until, setUntil] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (until <= Date.now()) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [until]);
  const remaining = Math.max(0, Math.ceil((until - now) / 1000));
  const apply = useCallback((info?: WorkflowRefreshInfo | null) => {
    const seconds = info?.retry_after ?? 0;
    setNow(Date.now());
    setUntil(seconds > 0 ? Date.now() + seconds * 1000 : 0);
  }, []);
  return [remaining, apply];
}

/**
 * Push channel for status changes (the backend sends only when the snapshot or pending rows
 * change on the GitHub Actions backend; it never starts a status run).
 */
export function useWorkflowLiveSocket(enabled: boolean, onSummary: (summary: WorkflowSummaryPayload) => void) {
  const handler = useRef(onSummary);
  handler.current = onSummary;
  useEffect(() => {
    if (!enabled) return;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let closed = false;
    const connect = () => {
      try {
        socket = new WebSocket(workflowSocketUrl());
      } catch {
        return;
      }
      socket.onmessage = (event) => {
        try { handler.current(JSON.parse(event.data) as WorkflowSummaryPayload); } catch { /* ignore malformed frames */ }
      };
      // 1000 = the server closed on purpose (e.g. a monitor-only runtime): do not reconnect.
      socket.onclose = (event) => { if (!closed && event.code !== 1000) reconnectTimer = window.setTimeout(connect, 3_000); };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => { closed = true; window.clearTimeout(reconnectTimer); socket?.close(); };
  }, [enabled]);
}
