export const API_BASE = import.meta.env.VITE_BACKFILL_API_BASE_URL ?? '';
export const API_CACHE_PREFIX = 'ulrpm-api-cache:';
export const TOAST_DURATION_MS = 4500;
/** Sandbox tab (local command builder, no backend calls). Hidden unless VITE_ENABLE_SANDBOX=1 at build time. */
export const SANDBOX_ENABLED = import.meta.env.VITE_ENABLE_SANDBOX === '1';
