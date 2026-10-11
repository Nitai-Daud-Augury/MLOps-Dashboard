import { AlertTriangle, ChevronDown, ChevronUp, Info, X } from 'lucide-react';
import { useState } from 'react';

import { SOURCE_NAMES, type FeaturesSourceStatus } from './featuresSourceApi';

/**
 * Persistent mismatch banner + databricks-mode notes (shown on every tab).
 * - Rescan required: can be collapsed to one line but never hidden while it is
 *   still relevant; it re-expands when the mismatch changes (new switch or scan).
 * - Databricks note / not-durable note: dismissible; they come back after the
 *   next switch.
 */
export function FeaturesSourceBanner({ status, onRescan }: { status: FeaturesSourceStatus | null | undefined; onRescan: () => void | Promise<void> }) {
  const [collapsedKey, setCollapsedKey] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<Record<string, string>>({});
  if (!status) return null;
  const active = status.active_source ?? status.source;
  const mismatchKey = `${status.results_source}|${active}|${status.results_scan_id ?? ''}|${status.updated_at ?? ''}`;
  const collapsed = collapsedKey === mismatchKey;
  const switchKey = `${active}|${status.updated_at ?? ''}`;
  const notDurable = status.origin === 'switched' && status.persistence?.durable === false;
  const showNote = active === 'databricks' && dismissed.note !== switchKey;
  const showDurable = notDurable && dismissed.durable !== switchKey;
  const dismiss = (name: string) => setDismissed((current) => ({ ...current, [name]: switchKey }));
  return <>
    {status.mismatch && status.results_source ? (
      <section className={`notice features-source-banner${collapsed ? ' collapsed' : ''}`} role="status">
        <AlertTriangle size={17} />
        <span>
          <strong>Results were scanned from {SOURCE_NAMES[status.results_source]}; source is now {SOURCE_NAMES[active]} — rescan required.</strong>
          {!collapsed && status.results_source_inferred ? ' (Results predate the source switch and were read from blob.)' : ''}
        </span>
        <div className="notice-actions">
          <button className="primary-button" type="button" onClick={() => void onRescan()} disabled={status.scan_running}>
            <span>{status.scan_running ? 'Scan running…' : 'Rescan now'}</span>
          </button>
          <button className="notice-dismiss" type="button" onClick={() => setCollapsedKey(collapsed ? null : mismatchKey)}
            aria-label={collapsed ? 'Expand rescan warning' : 'Collapse rescan warning'} title={collapsed ? 'Expand' : 'Collapse'}>
            {collapsed ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
          </button>
        </div>
      </section>
    ) : null}
    {showNote ? (
      <section className="notice features-source-note" role="note">
        <Info size={17} />
        <span><strong>Reading from Databricks feature_store.</strong> The Databricks copy may be stale for older backfilled months (bronze ingests only the current and previous month), and the schema completeness check is unavailable.</span>
        <button className="notice-dismiss" type="button" onClick={() => dismiss('note')} aria-label="Dismiss Databricks note" title="Dismiss"><X size={16} /></button>
      </section>
    ) : null}
    {showDurable ? (
      <section className="notice features-source-note" role="note">
        <Info size={17} />
        <span>The source switch could not be saved to the control store ({status.persistence.error ?? 'unavailable'}); it applies until the app restarts, then the default ({SOURCE_NAMES[status.default]}) returns.</span>
        <button className="notice-dismiss" type="button" onClick={() => dismiss('durable')} aria-label="Dismiss not-saved note" title="Dismiss"><X size={16} /></button>
      </section>
    ) : null}
  </>;
}
