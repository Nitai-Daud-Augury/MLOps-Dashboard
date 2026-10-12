import { useCallback, useEffect, useRef, useState } from 'react';
import { API_BASE } from '../constants';
import { readCachedJson, writeCachedJson } from '../utils/cache';

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

/** The FastAPI `detail` (string or object) of an error response, else the status line. */
export async function readDetail(response: Response): Promise<string> {
  try {
    const body = await response.json() as { detail?: unknown };
    if (typeof body.detail === 'string') return body.detail;
    if (body.detail && typeof body.detail === 'object') {
      const detail = body.detail as { message?: unknown; error?: unknown };
      if (typeof detail.message === 'string') return detail.message;
      if (typeof detail.error === 'string') return detail.error;
    }
    return JSON.stringify(body.detail ?? body);
  } catch {
    return `${response.status} ${response.statusText}`.trim();
  }
}

export async function apiJson<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  const { json, ...rest } = init ?? {};
  const response = await fetch(`${API_BASE}${path}`, {
    cache: 'no-store',
    ...rest,
    ...(json !== undefined ? { body: JSON.stringify(json), headers: { 'Content-Type': 'application/json', ...(rest.headers ?? {}) } } : {}),
  });
  if (!response.ok) throw new ApiError(await readDetail(response), response.status);
  return await response.json() as T;
}

export const postJson = <T>(path: string, json: unknown = {}) => apiJson<T>(path, { method: 'POST', json });

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export interface Resource<T> {
  data: T | null;
  error: string | null;
  /** First load with nothing to show yet (render a skeleton). */
  loading: boolean;
  /** A reload while older data stays visible (stale-while-revalidate). */
  refreshing: boolean;
  updatedAt: number | null;
  reload: () => Promise<T | null>;
  set: (value: T) => void;
}

/**
 * GET with stale-while-revalidate: a cached copy (sessionStorage, maxAgeMs) renders at once,
 * then the request refreshes it. Errors keep the last good data.
 */
export function useResource<T>(path: string | null, { cacheKey, maxAgeMs = 60_000 }: { cacheKey?: string; maxAgeMs?: number } = {}): Resource<T> {
  const key = cacheKey ?? path ?? '';
  const [data, setData] = useState<T | null>(() => (path ? readCachedJson<T>(key, maxAgeMs) : null));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const seq = useRef(0);
  const reload = useCallback(async () => {
    if (!path) return null;
    const mine = ++seq.current;
    setBusy(true);
    try {
      const value = await apiJson<T>(path);
      if (mine === seq.current) { setData(value); setError(null); setUpdatedAt(Date.now()); writeCachedJson(key, value); }
      return value;
    } catch (exc) {
      if (mine === seq.current) setError(errorText(exc));
      return null;
    } finally {
      if (mine === seq.current) setBusy(false);
    }
  }, [path, key]);
  useEffect(() => { void reload(); }, [reload]);
  const set = useCallback((value: T) => { setData(value); setUpdatedAt(Date.now()); writeCachedJson(key, value); }, [key]);
  return { data, error, loading: busy && data === null, refreshing: busy && data !== null, updatedAt, reload, set };
}
