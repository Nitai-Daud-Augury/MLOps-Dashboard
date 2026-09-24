import { API_BASE, API_CACHE_PREFIX } from '../constants';

export function readCachedJson<T>(key: string, maxAgeMs: number): T | null {
  try {
    const cached = JSON.parse(sessionStorage.getItem(`${API_CACHE_PREFIX}${key}`) ?? '') as { savedAt: number; value: T };
    return Date.now() - cached.savedAt <= maxAgeMs ? cached.value : null;
  } catch { return null; }
}

export function writeCachedJson<T>(key: string, value: T) {
  try { sessionStorage.setItem(`${API_CACHE_PREFIX}${key}`, JSON.stringify({ savedAt: Date.now(), value })); } catch { /* optional cache */ }
}

export async function getCachedJson<T>(key: string, url: string, maxAgeMs: number, force = false): Promise<T> {
  const cached = force ? null : readCachedJson<T>(key, maxAgeMs);
  if (cached !== null) return cached;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  const value = await response.json() as T;
  writeCachedJson(key, value);
  return value;
}

export function workflowSocketUrl() {
  const url = new URL(API_BASE);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = `${url.pathname.replace(/\/$/, '')}/api/admin/workflows/live`;
  return url.toString();
}
