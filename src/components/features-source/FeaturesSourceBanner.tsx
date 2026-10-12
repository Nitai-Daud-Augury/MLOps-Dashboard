import { ChevronDown, ChevronUp } from 'lucide-react';
import { useState } from 'react';
import { Banner } from '../ui';
import { useStatus } from '../../state/statusStore';
import { SOURCE_NAMES } from './featuresSourceApi';

/**
 * Rescan-required banner (results from another source): collapsible but never hidden while
 * relevant; re-expands when the mismatch changes. Databricks / not-durable notes dismiss
 * until the next switch.
 */
export function FeaturesSourceBanner() {
  const { payload, startScan, scanRequesting } = useStatus();
  const status = payload?.data_sources?.features_source;
  const [collapsedKey, setCollapsedKey] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<Record<string, string>>({});
  if (!status) return null;
  const active = status.active_source ?? status.source;
  const mismatchKey = `${status.results_source}|${active}|${status.results_scan_id ?? ''}|${status.updated_at ?? ''}`;
  const collapsed = collapsedKey === mismatchKey;
  const switchKey = `${active}|${status.updated_at ?? ''}`;
  const notDurable = status.origin === 'switched' && status.persistence?.durable === false;
  const running = status.scan_running || Boolean(payload?.scan_state.running);
  return <>
    {status.mismatch && status.results_source ? (
      <Banner tone="warn" title={`Results were scanned from ${SOURCE_NAMES[status.results_source]}; source is now ${SOURCE_NAMES[active]} — rescan required.`}
        actions={<>
          <button type="button" className="btn btn-primary" onClick={() => void startScan()} disabled={running || scanRequesting}>{running ? 'Scan running…' : 'Rescan now'}</button>
          <button type="button" className="icon-btn" onClick={() => setCollapsedKey(collapsed ? null : mismatchKey)} aria-label={collapsed ? 'Expand rescan warning' : 'Collapse rescan warning'}>
            {collapsed ? <ChevronDown size={14} /> : <ChevronUp size={14} />}</button>
        </>}>
        {collapsed ? null : status.results_source_inferred ? 'The results predate the source switch and were read from blob.' : 'The table below still shows the previous source until a scan completes.'}
      </Banner>
    ) : null}
    {active === 'databricks' && dismissed.note !== switchKey ? (
      <Banner tone="info" title="Reading from Databricks feature_store." onDismiss={() => setDismissed((d) => ({ ...d, note: switchKey }))}>
        The copy may be stale for older backfilled months (bronze ingests only the current and previous month), and the schema completeness check is unavailable.
      </Banner>
    ) : null}
    {notDurable && dismissed.durable !== switchKey ? (
      <Banner tone="warn" title="Source switch not saved durably." onDismiss={() => setDismissed((d) => ({ ...d, durable: switchKey }))}>
        Control store unavailable ({status.persistence.error ?? 'unknown error'}); the switch applies until the app restarts, then the default ({SOURCE_NAMES[status.default]}) returns.
      </Banner>
    ) : null}
  </>;
}
