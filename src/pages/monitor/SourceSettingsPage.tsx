import { useState } from 'react';
import { FeaturesSourceControl } from '../../components/features-source/FeaturesSourceControl';
import { SOURCE_NAMES } from '../../components/features-source/featuresSourceApi';
import { Spinner } from '../../components/ui';
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
  return <>
    <div className="page-head"><div><h1>Data source</h1><span className="muted">Where scans read Feature Store coverage from. Switching affects every user and requires a rescan.</span></div></div>
    <section className="panel">
      <div className="panel-head"><h2>Features source</h2></div>
      <FeaturesSourceControl placement="page" canSwitch />
      {status ? <dl className="dl">
        <dt>Active</dt><dd>{SOURCE_NAMES[status.active_source]} <span className="muted">({status.origin.replace('_', ' ')}{status.updated_by ? ` by ${status.updated_by}` : ''}{status.updated_at ? `, ${exactTime(status.updated_at)}` : ''})</span></dd>
        <dt>Default</dt><dd>{SOURCE_NAMES[status.default]} (FEATURES_SOURCE)</dd>
        <dt>Current results</dt><dd>{status.results_source ? `${SOURCE_NAMES[status.results_source]} · scanned ${exactTime(status.results_scanned_at)}` : 'none yet'}</dd>
        <dt>Saved in</dt><dd className="wrap">{status.persistence?.location ?? '—'}{status.persistence?.durable === false ? ' (not durable)' : ''}</dd>
        {status.config_error ? <><dt>Config error</dt><dd className="field-error">{status.config_error}</dd></> : null}
      </dl> : <p className="muted"><Spinner /> Loading…</p>}
      {status?.notes?.length ? <ul>{status.notes.map((note) => <li key={note} className="wrap">{note}</li>)}</ul> : null}
    </section>
    <section className="panel">
      <div className="panel-head"><h2>Blob location (blob scans)</h2></div>
      <p className="muted">Stored in this browser only; sent with the next scan request.</p>
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
    </section>
  </>;
}
