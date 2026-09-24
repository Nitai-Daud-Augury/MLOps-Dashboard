import { API_BASE } from '../../constants';
import type { Campaign, ControlStatus, Estimate, InventoryFilters, InventoryPage } from './types';

async function json<T>(response: Response): Promise<T> {
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail ?? `Request failed (${response.status})`);
  return payload as T;
}
export function inventoryPage(filters: InventoryFilters, cursor: string | null, signal: AbortSignal) {
  const params = new URLSearchParams({ limit: '50' });
  Object.entries(filters).forEach(([key, value]) => value && params.set(key, value));
  if (cursor) params.set('cursor', cursor);
  return fetch(`${API_BASE}/api/v1/machines?${params}`, { signal }).then(json<InventoryPage>);
}
export function requestEstimate(body: object) {
  return fetch(`${API_BASE}/api/v1/backfill-estimates`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(json<Estimate>);
}
export function submitCampaign(body: object) {
  return fetch(`${API_BASE}/api/v1/backfill-campaigns`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(json<Campaign>);
}
export function getCampaign(id: string) {
  return fetch(`${API_BASE}/api/v1/backfill-campaigns/${encodeURIComponent(id)}`).then(json<Campaign>);
}
export function campaignAction(id: string, action: string) {
  return fetch(`${API_BASE}/api/v1/backfill-campaigns/${encodeURIComponent(id)}/actions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) }).then(json<Campaign>);
}
export function getControlStatus() {
  return fetch(`${API_BASE}/api/v1/control-plane/status`).then(json<ControlStatus>);
}
export function listCampaigns() {
  return fetch(`${API_BASE}/api/v1/backfill-campaigns?limit=8`).then(json<{ campaigns: Campaign[] }>);
}
