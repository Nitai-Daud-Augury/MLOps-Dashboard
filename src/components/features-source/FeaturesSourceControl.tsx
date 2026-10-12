import { Database, HardDrive } from 'lucide-react';
import { useState } from 'react';
import { ConfirmDialog, Spinner } from '../ui';
import { useToast } from '../ui/toastContext';
import { exactTime } from '../../lib/format';
import { useStatus } from '../../state/statusStore';
import { putFeaturesSource, SOURCE_NAMES, type FeaturesSource } from './featuresSourceApi';

/**
 * Features-source switch (Blob / Databricks), self-contained: reads the status from
 * StatusContext, PUTs /api/settings/features-source behind a confirm dialog (a rescan is
 * required), and offers "Switch and rescan now".
 *
 * placement: "header" = compact segmented control; "page" = full control with explanations.
 * canSwitch: false renders a read-only indicator (no PUT possible).
 */
export function FeaturesSourceControl({ placement, canSwitch }: { placement: 'header' | 'page'; canSwitch: boolean }) {
  const { payload, reload, startScan } = useStatus();
  const notify = useToast();
  const status = payload?.data_sources?.features_source;
  const [pending, setPending] = useState<FeaturesSource | null>(null);
  const [switching, setSwitching] = useState<FeaturesSource | null>(null);
  const [error, setError] = useState<string | null>(null);
  const active = switching ?? status?.active_source ?? status?.source;
  const options: FeaturesSource[] = status?.options ?? ['blob', 'databricks'];
  const scanRunning = Boolean(status?.scan_running || payload?.scan_state.running);

  const confirm = async (rescan: boolean) => {
    if (!pending) return;
    const target = pending;
    setSwitching(target);
    setError(null);
    try {
      const result = await putFeaturesSource(target);
      notify('success', `Features source is now ${SOURCE_NAMES[target]}.${result.persistence?.durable === false ? ' Not saved durably (see banner).' : ''}`);
      setPending(null);
      await reload();
      if (rescan) await startScan();
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      setError(message);
      notify('error', `Switch failed: ${message}`);
    } finally { setSwitching(null); }
  };

  if (!status) {
    return <span className={`fs-control fs-${placement} muted`}>{placement === 'page' ? 'Loading features source…' : <Spinner label="Loading features source" />}</span>;
  }
  if (!canSwitch) {
    return <span className={`fs-control fs-${placement}`} title="Features source (read-only here)">Source: <strong>{SOURCE_NAMES[status.active_source]}</strong></span>;
  }
  return <>
    <div className={`fs-control fs-${placement}`} role="radiogroup" aria-label="Features source">
      {placement === 'header' ? <span className="fs-label">Source</span> : null}
      {options.map((option) => {
        const Icon = option === 'blob' ? HardDrive : Database;
        return <button key={option} type="button" role="radio" aria-checked={active === option}
          className={`seg${active === option ? ' seg-on' : ''}`} disabled={Boolean(switching) || scanRunning}
          title={scanRunning ? 'A scan is running; switch after it finishes.' : `Read Feature Store coverage from ${SOURCE_NAMES[option]}`}
          onClick={() => { if (option !== active) { setError(null); setPending(option); } }}>
          {switching === option ? <Spinner /> : <Icon size={13} aria-hidden="true" />}{SOURCE_NAMES[option]}
        </button>;
      })}
      {switching ? <span className="muted" role="status">Switching…</span> : null}
    </div>
    <ConfirmDialog open={Boolean(pending)} title={`Switch features source to ${pending ? SOURCE_NAMES[pending] : ''}?`}
      confirmLabel="Switch and rescan now" busy={Boolean(switching)} error={error}
      onConfirm={() => void confirm(true)} onCancel={() => setPending(null)}
      extraActions={<button type="button" className="btn" disabled={Boolean(switching)} onClick={() => void confirm(false)}>Switch only</button>}>
      <p>{status.results_source && status.results_source === pending
        ? <>The current results were already scanned from <strong>{SOURCE_NAMES[pending]}</strong>; switching back clears the rescan warning.</>
        : status.results_source
        ? <>Current results were scanned from <strong>{SOURCE_NAMES[status.results_source]}</strong> ({exactTime(status.results_scanned_at)} IL). They stay visible but <strong>a rescan is required</strong> before they reflect {pending ? SOURCE_NAMES[pending] : ''}.</>
        : <>No scan results yet. The next scan reads from {pending ? SOURCE_NAMES[pending] : ''}.</>}</p>
      {pending === 'databricks' ? <ul>
        <li>The Databricks copy may be stale for older backfilled months (bronze ingests only the current and previous month).</li>
        <li>The schema completeness check is not available in Databricks mode.</li>
        <li>The blob-vs-Databricks cross-check is disabled while Databricks is selected.</li>
      </ul> : null}
      <p className="muted">This setting is global for every user. Running backfills and admin actions are not affected.</p>
    </ConfirmDialog>
  </>;
}
