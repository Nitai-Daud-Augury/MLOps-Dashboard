/** Builds the Data source page links from /api/backfill/status data_sources (no secrets, no SAS). */
export interface SourceLinksConfig {
  databricks_host?: string | null;
  feature_store_table?: string | null;
  test_machines_source?: string | null;
  repo?: string | null;
  repo_ref?: string | null;
}

export interface SourceLink { label: string; value: string; href: string | null; help: string }

const seg = (part: string) => encodeURIComponent(part);
const path = (value: string) => value.split('/').filter(Boolean).map(seg).join('/');

/** https://<account>.blob.core.windows.net/<container>/<prefix>: plain endpoint, never a SAS. */
export function blobUrl(account?: string | null, container?: string | null, prefix = ''): string | null {
  if (!account || !/^[a-z0-9]{3,24}$/.test(account) || !container) return null;
  return `https://${account}.blob.core.windows.net/${seg(container)}${prefix ? `/${path(prefix)}` : ''}`;
}

/** "azure_blob:<account>/<container>/<path>" → blob URL. */
export function blobLocationUrl(location?: string | null): string | null {
  const match = /^azure_blob:([^/]+)\/([^/]+)\/?(.*)$/.exec(location ?? '');
  return match ? blobUrl(match[1], match[2], match[3]) : null;
}

/** <host>/explore/data/<catalog>/<schema>/<table> (Unity Catalog explorer). */
export function databricksTableUrl(host?: string | null, table?: string | null): string | null {
  const parts = (table ?? '').split('.');
  if (!host || !/^https:\/\/[^/?#]+$/.test(host) || parts.length !== 3 || parts.some((p) => !p)) return null;
  return `${host}/explore/data/${parts.map(seg).join('/')}`;
}

/** "file:data/x.txt" or "docs/UI.md" → GitHub blob URL; null for absolute/non-repo files. */
export function repoFileUrl(repo?: string | null, ref?: string | null, file?: string | null): string | null {
  const rel = (file ?? '').replace(/^file:/, '');
  if (!repo || !/^[\w.-]+\/[\w.-]+$/.test(repo) || !rel || rel.startsWith('/') || rel.includes('..')) return null;
  return `https://github.com/${repo}/blob/${seg(ref || 'main')}/${path(rel)}`;
}
