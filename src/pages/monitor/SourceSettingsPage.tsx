import { useState } from 'react';
import { FeaturesSourceControl } from '../../components/features-source/FeaturesSourceControl';
import { SOURCE_NAMES } from '../../components/features-source/featuresSourceApi';
import { Database, ExternalLink, HardDrive } from 'lucide-react';
import { blobLocationUrl, blobUrl, databricksTableUrl, repoFileUrl, type SourceLink } from '../../features/source/sourceLinks';
import { Spinner } from '../../components/ui';
import { PageHeader, Section } from '../../components/ui/Layout';
import { useResource } from '../../lib/api';
import { exactTime } from '../../lib/format';
import { useStatus } from '../../state/statusStore';

/** Data source settings: the features-source switch plus the blob account/container used by blob scans. */
export function SourceSettingsPage() {
  const { payload, blobSource, setBlobSource } = useStatus();
  const status = payload?.data_sources?.features_source;
  const [account, setAccount] = useState(blobSource.account);
  const [container, setContainer] = useState(blobSource.container);
  const accounts = useResource<{ accounts: string[] }>('/api/blob-sources/accounts', { cacheKey: 'blob-accounts', maxAgeMs: 60 * 60_000 });
  const containers = useResource<{ containers?: string[] }>(account ? `/api/blob-sources/containers?account=${encodeURIComponent(account)}` : null, { cacheKey: `blob-containers:${account}`, maxAgeMs: 60 * 60_000 });
  const dirty = account !== blobSource.account || container !== blobSource.container;
  const activeSource = status?.active_source;
  const ds = payload?.data_sources;
  const cfg = ds?.links ?? {};
  const snap = payload?.snapshot;
  const blobAccount = blobSource.account || snap?.source_account;
  const blobContainer = blobSource.container || snap?.source_container;
  const repoFile = (file?: string | null) => repoFileUrl(cfg.repo, cfg.repo_ref, file);
  const dbx = (table?: string | null) => databricksTableUrl(cfg.databricks_host, table);
  const links: SourceLink[] = [
    { label: 'Feature files', value: [blobAccount, blobContainer].filter(Boolean).join(' / '), href: blobUrl(blobAccount, blobContainer), help: 'Backfilled parquet files read by blob scans.' },
    { label: 'Databricks features', value: cfg.feature_store_table ?? ds?.features_crosscheck?.table ?? '', href: dbx(cfg.feature_store_table ?? ds?.features_crosscheck?.table), help: 'Feature table read when the source is Databricks.' },
    { label: 'Install dates', value: ds?.lifecycle_table ?? '', href: dbx(ds?.lifecycle_table), help: 'Machine lifecycle table (first expected month).' },
    { label: 'Machine list', value: ds?.inventory_source?.replace(/^file:/, '') ?? '', href: repoFile(ds?.inventory_source), help: 'Machines included in scans.' },
    { label: 'Test machines', value: cfg.test_machines_source?.replace(/^file:/, '') ?? '', href: repoFile(cfg.test_machines_source), help: 'Machines hidden by "Hide test machines".' },
    { label: 'Source setting', value: 'features_source.json', href: blobLocationUrl(status?.persistence?.location), help: 'Where the switch below is saved.' },
    { label: 'Docs', value: 'docs/UI.md', href: repoFile('docs/UI.md'), help: 'How the dashboard works.' },
  ].filter((link) => link.value);
  return <>
    <PageHeader title="Data source" purpose="Where coverage scans read their data, with a link to every input." />
    <div className="option-cards" aria-label="Available sources">
      <div className="option-card" aria-checked={activeSource === 'blob'} role="note" style={{ cursor: 'default' }}>
        <span className="option-title"><HardDrive size={18} aria-hidden="true" />Azure Blob {activeSource === 'blob' ? <span className="badge tone-success">Active</span> : null}</span>
        <span className="helper">Reads the backfilled parquet files directly. Complete for every month, includes the schema check and the blob-vs-Databricks cross-check.</span>
      </div>
      <div className="option-card" aria-checked={activeSource === 'databricks'} role="note" style={{ cursor: 'default' }}>
        <span className="option-title"><Database size={18} aria-hidden="true" />Databricks {activeSource === 'databricks' ? <span className="badge tone-success">Active</span> : null}</span>
        <span className="helper">Reads the Databricks copy. Faster, but older backfilled months may be stale (bronze ingests only the current and previous month); no schema check.</span>
      </div>
    </div>
    <Section title="Inputs" description="Open each location in Azure, Databricks or GitHub (sign-in may be required).">
      {payload ? <ul className="source-links">{links.map((link) => <li key={link.label}>
        <span className="source-link-label">{link.label}<span className="helper">{link.help}</span></span>
        <code className="wrap">{link.value}</code>
        {link.href ? <a className="btn btn-sm" href={link.href} target="_blank" rel="noreferrer noopener">Open <ExternalLink size={14} aria-hidden="true" /></a> : <span className="muted">No link</span>}
      </li>)}</ul> : <p className="muted"><Spinner /> Loading…</p>}
    </Section>
    <Section title="Features source" description="Switching applies to everyone and needs a rescan.">
      <FeaturesSourceControl placement="page" canSwitch />
      {status ? <dl className="dl">
        <dt>Active</dt><dd>{SOURCE_NAMES[status.active_source]} <span className="muted">({status.origin.replace('_', ' ')}{status.updated_by ? ` by ${status.updated_by}` : ''}{status.updated_at ? `, ${exactTime(status.updated_at)}` : ''})</span></dd>
        <dt>Default</dt><dd>{SOURCE_NAMES[status.default]} (FEATURES_SOURCE)</dd>
        <dt>Current results</dt><dd>{status.results_source ? `${SOURCE_NAMES[status.results_source]} · scanned ${exactTime(status.results_scanned_at)}` : 'none yet'}</dd>
        {status.persistence?.durable === false ? <><dt>Saved in</dt><dd>This server only (not durable)</dd></> : null}
        {status.config_error ? <><dt>Config error</dt><dd className="field-error">{status.config_error}</dd></> : null}
      </dl> : <p className="muted"><Spinner /> Loading…</p>}
      {status?.notes?.length ? <ul>{status.notes.map((note) => <li key={note} className="wrap">{note}</li>)}</ul> : null}
    </Section>
    <Section title="Blob location" description="Used by blob scans. Saved in this browser only.">
      <div className="form-row">
        <label className="field">Storage account
          {accounts.data?.accounts?.length ? <select value={account} onChange={(e) => { setAccount(e.target.value); setContainer(''); }}>
            {[...new Set([account, ...accounts.data.accounts])].filter(Boolean).map((name) => <option key={name}>{name}</option>)}
          </select> : <input value={account} onChange={(e) => setAccount(e.target.value)} />}
        </label>
        <label className="field">Container {containers.loading ? <Spinner /> : null}
          {containers.data?.containers?.length ? <select value={container} onChange={(e) => setContainer(e.target.value)}>
            <option value="" disabled>Choose…</option>
            {[...new Set([container, ...containers.data.containers])].filter(Boolean).map((name) => <option key={name}>{name}</option>)}
          </select> : <input value={container} onChange={(e) => setContainer(e.target.value)} />}
        </label>
        <button type="button" className="btn btn-primary" disabled={!dirty || !account || !container} onClick={() => setBlobSource({ account, container })}>Save</button>
      </div>
      {accounts.error ? <p className="muted">Account list unavailable ({accounts.error}); type the names instead.</p> : null}
    </Section>
  </>;
}
