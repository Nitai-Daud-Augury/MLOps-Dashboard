import { useState } from 'react';
import { FeaturesSourceControl } from '../../components/features-source/FeaturesSourceControl';
import { SOURCE_NAMES } from '../../components/features-source/featuresSourceApi';
import { Database, HardDrive } from 'lucide-react';
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
  return <>
    <PageHeader title="Data source" purpose="Choose where coverage scans read the Feature Store from. The setting is global for every user and needs a rescan." />
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
    <Section title="Features source" description="Switch the source here or from the Monitoring header. A confirm dialog explains the impact; results stay visible until you rescan.">
      <FeaturesSourceControl placement="page" canSwitch />
      {status ? <dl className="dl">
        <dt>Active</dt><dd>{SOURCE_NAMES[status.active_source]} <span className="muted">({status.origin.replace('_', ' ')}{status.updated_by ? ` by ${status.updated_by}` : ''}{status.updated_at ? `, ${exactTime(status.updated_at)}` : ''})</span></dd>
        <dt>Default</dt><dd>{SOURCE_NAMES[status.default]} (FEATURES_SOURCE)</dd>
        <dt>Current results</dt><dd>{status.results_source ? `${SOURCE_NAMES[status.results_source]} · scanned ${exactTime(status.results_scanned_at)}` : 'none yet'}</dd>
        <dt>Saved in</dt><dd className="wrap">{status.persistence?.location ?? '—'}{status.persistence?.durable === false ? ' (not durable)' : ''}</dd>
        {status.config_error ? <><dt>Config error</dt><dd className="field-error">{status.config_error}</dd></> : null}
      </dl> : <p className="muted"><Spinner /> Loading…</p>}
      {status?.notes?.length ? <ul>{status.notes.map((note) => <li key={note} className="wrap">{note}</li>)}</ul> : null}
    </Section>
    <Section title="Blob location" description="Storage account and container used by blob scans. Stored in this browser only and sent with the next scan request.">
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
