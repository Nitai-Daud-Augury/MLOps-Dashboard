import { AlertTriangle, Database, HardDrive, Loader2, RefreshCw, X } from 'lucide-react';
import { useState } from 'react';

import { putFeaturesSource, SOURCE_NAMES, type FeaturesSource, type FeaturesSourceStatus } from './featuresSourceApi';

function when(value?: string | null): string {
  if (!value) return 'an earlier scan';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

/** Header selector: Blob / Databricks, with a confirm dialog because a rescan is required. */
export function FeaturesSourceSwitch({ status, onSwitched, onRescan, onToast }: {
  status: FeaturesSourceStatus | null | undefined;
  onSwitched: () => void | Promise<void>;
  onRescan: () => void | Promise<void>;
  onToast: (type: 'success' | 'error' | 'info', message: string) => void;
}) {
  const [pending, setPending] = useState<FeaturesSource | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = status?.active_source ?? status?.source;
  const options: FeaturesSource[] = status?.options ?? ['blob', 'databricks'];

  const confirm = async (rescan: boolean) => {
    if (!pending) return;
    setBusy(true);
    setError(null);
    try {
      const result = await putFeaturesSource(pending);
      onToast('success', `Features source is now ${SOURCE_NAMES[pending]}.${result.persistence?.durable === false ? ' Not saved durably; see the banner.' : ''}`);
      setPending(null);
      await onSwitched();
      if (rescan) await onRescan();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return <>
    <div className="features-source-switch" role="radiogroup" aria-label="Features source">
      <span className="features-source-label">Features</span>
      {options.map((option) => (
        <button
          key={option}
          type="button"
          role="radio"
          aria-checked={active === option}
          className={active === option ? 'active' : ''}
          disabled={!status || busy || status.scan_running}
          title={status?.scan_running ? 'A scan is running; switch after it finishes.' : `Read Feature Store coverage from ${SOURCE_NAMES[option]}`}
          onClick={() => { if (option !== active) { setError(null); setPending(option); } }}
        >
          {option === 'blob' ? <HardDrive size={14} /> : <Database size={14} />}
          <span>{SOURCE_NAMES[option]}</span>
        </button>
      ))}
    </div>
    {pending && status ? (
      <div className="modal-backdrop" role="presentation" onMouseDown={() => !busy && setPending(null)}>
        <section className="manifest-dialog features-source-dialog" role="dialog" aria-modal="true" aria-labelledby="features-source-dialog-title" onMouseDown={(event) => event.stopPropagation()}>
          <div className="panel-header">
            <div><p className="eyebrow">Change features source</p><h2 id="features-source-dialog-title">Switch to {SOURCE_NAMES[pending]}?</h2></div>
            <button className="icon-button" type="button" onClick={() => setPending(null)} disabled={busy} title="Cancel"><X size={18} /></button>
          </div>
          <p className="features-source-dialog-warning"><AlertTriangle size={16} />
            <span>{status.results_source
              ? <>The current results were scanned from <strong>{SOURCE_NAMES[status.results_source]}</strong> ({when(status.results_scanned_at)}). They stay on screen but come from the previous source: <strong>a rescan is required</strong> before they reflect {SOURCE_NAMES[pending]}.</>
              : <>No scan results yet. The next scan will read from {SOURCE_NAMES[pending]}.</>}</span>
          </p>
          {pending === 'databricks' ? (
            <ul className="features-source-dialog-notes">
              <li>The Databricks copy may be stale for older backfilled months (bronze ingests only the current and previous month).</li>
              <li>The schema completeness check is not available in Databricks mode.</li>
              <li>The blob-vs-Databricks cross-check is disabled while Databricks is selected.</li>
            </ul>
          ) : null}
          <p className="features-source-dialog-small">Running backfills and admin actions are not affected.</p>
          {error ? <p className="features-source-dialog-error" role="alert">{error}</p> : null}
          <div className="actions dialog-actions">
            <button className="secondary-button" type="button" onClick={() => setPending(null)} disabled={busy}>Cancel</button>
            <button className="secondary-button" type="button" onClick={() => void confirm(false)} disabled={busy}>{busy ? <Loader2 className="spin" size={16} /> : null}<span>Switch to {SOURCE_NAMES[pending]}</span></button>
            <button className="primary-button" type="button" onClick={() => void confirm(true)} disabled={busy}><RefreshCw size={16} /><span>Switch and rescan now</span></button>
          </div>
        </section>
      </div>
    ) : null}
  </>;
}
