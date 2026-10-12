import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { StatusContext, type BlobSource, type StatusContextValue } from './statusStore';
import { BLOB_SOURCE_STORAGE_KEY, PROD_ACCOUNT, PROD_CONTAINER } from '../constants';
import { errorText, postJson, useResource } from '../lib/api';
import { useToast } from '../components/ui/toastContext';
import type { RuntimeInfo, StatusPayload } from '../types';

export type { BlobSource };

function readBlobSource(): BlobSource {
  try {
    const parsed = JSON.parse(localStorage.getItem(BLOB_SOURCE_STORAGE_KEY) ?? '') as Partial<BlobSource>;
    return { account: parsed.account || PROD_ACCOUNT, container: parsed.container || PROD_CONTAINER };
  } catch { return { account: PROD_ACCOUNT, container: PROD_CONTAINER }; }
}

/** /api/backfill/status (scan state, snapshot, data sources, persistence) shared by both route trees. */
export function StatusProvider({ children }: { children: ReactNode }) {
  const notify = useToast();
  const status = useResource<StatusPayload>('/api/backfill/status', { cacheKey: 'backfill-status', maxAgeMs: 5 * 60_000 });
  const health = useResource<{ runtime?: RuntimeInfo }>('/api/health', { cacheKey: 'health', maxAgeMs: 0 });
  const [scanRequesting, setScanRequesting] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [blobSource, setBlobSourceState] = useState(readBlobSource);
  const { reload } = status;
  const payload = status.data;
  const running = Boolean(payload?.scan_state.running);
  const restoring = Boolean(payload?.persistence?.loading);

  // Poll only while a scan runs (5s) or the saved scan is still loading after a restart (3s).
  useEffect(() => {
    if (!running && !restoring) return;
    const id = window.setInterval(() => void reload(), running ? 5_000 : 3_000);
    return () => window.clearInterval(id);
  }, [running, restoring, reload]);

  const [wasRunning, setWasRunning] = useState(running);
  useEffect(() => {
    if (wasRunning && !running && payload) {
      if (payload.scan_state.error) { setScanError(payload.scan_state.error); notify('error', 'Scan failed. See the banner for details.'); }
      else notify('success', `Scan finished: ${payload.snapshot.machines.length} machines.`);
    }
    setWasRunning(running);
  }, [running, wasRunning, payload, notify]);

  const startScan = useCallback(async () => {
    setScanRequesting(true);
    setScanError(null);
    try {
      const source = payload?.data_sources?.features_source?.active_source === 'databricks' ? 'Databricks' : 'blob';
      const result = await postJson<{ already_running?: boolean }>('/api/backfill/scan', { source_account: blobSource.account, source_container: blobSource.container });
      notify('info', result.already_running ? 'A scan is already running.' : `Scan started (${source}).`);
      await reload();
    } catch (exc) {
      setScanError(errorText(exc));
      notify('error', 'Scan request failed.');
    } finally { setScanRequesting(false); }
  }, [blobSource, payload, notify, reload]);

  const resetScan = useCallback(async () => {
    try {
      await postJson('/api/backfill/scan/reset');
      notify('success', 'Scan state reset.');
      await reload();
    } catch (exc) { notify('error', `Reset failed: ${errorText(exc)}`); }
  }, [notify, reload]);

  const setBlobSource = useCallback((source: BlobSource) => {
    setBlobSourceState(source);
    localStorage.setItem(BLOB_SOURCE_STORAGE_KEY, JSON.stringify(source));
  }, []);

  const value = useMemo<StatusContextValue>(() => ({
    payload, error: status.error, loading: status.loading, refreshing: status.refreshing, reload,
    startScan, resetScan, scanRequesting, scanError, clearScanError: () => setScanError(null),
    blobSource, setBlobSource,
    runtime: health.data?.runtime ?? null,
    health: health.data ? 'ok' : health.error ? 'down' : 'checking',
  }), [payload, status.error, status.loading, status.refreshing, reload, startScan, resetScan, scanRequesting, scanError, blobSource, setBlobSource, health.data, health.error]);
  return <StatusContext.Provider value={value}>{children}</StatusContext.Provider>;
}
