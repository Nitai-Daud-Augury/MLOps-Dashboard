import { ExternalLink } from 'lucide-react';
import { CopyIdButton, StatusBadge, TestBadge } from '../../components/ui';
import { exactTime } from '../../lib/format';
import { BACKFILL_STATUS } from '../../lib/status';
import type { MachineStatus } from '../../types';

export function MachineTitle({ machine }: { machine: MachineStatus }) {
  return <span className="cell-id"><strong>{machine.display_name || machine.machine_id}</strong>
    {machine.is_test_machine ? <TestBadge /> : null}<CopyIdButton value={machine.machine_id} /></span>;
}

export function MachineStatusBadge({ machine }: { machine: MachineStatus }) {
  const status = BACKFILL_STATUS[machine.status] ?? BACKFILL_STATUS.unknown;
  return <StatusBadge tone={status.tone} label={status.label} title={machine.reason} />;
}

/** Key facts for the quick-view sheet and the Overview tab. */
export function MachineFacts({ machine }: { machine: MachineStatus }) {
  return <dl className="dl">
    <dt>Machine ID</dt><dd className="mono">{machine.machine_id}</dd>
    <dt>Status</dt><dd><MachineStatusBadge machine={machine} /> <span className="muted">{machine.reason}</span></dd>
    <dt>Coverage</dt><dd className="mono">{machine.months_complete}/{machine.months_expected} months</dd>
    <dt>First missing</dt><dd className="mono">{machine.first_missing_month ?? '—'}</dd>
    <dt>Last populated</dt><dd className="mono">{machine.last_populated_month ?? '—'}</dd>
    <dt>Installed</dt><dd>{machine.installation_at ? exactTime(machine.installation_at) : '—'}</dd>
    <dt>Rows</dt><dd className="mono">{(machine.production_rows ?? 0).toLocaleString('en-GB')}{typeof machine.silver_rows === 'number' ? ` (silver ${machine.silver_rows.toLocaleString('en-GB')})` : ''}</dd>
    <dt>Lifecycle</dt><dd>{machine.lifecycle_source ? `${machine.lifecycle_source}${machine.lifecycle_enriched ? '' : ' (no record)'}` : 'none'}</dd>
    {machine.crosscheck ? <><dt>Cross-check</dt><dd>{machine.crosscheck.status} · {machine.crosscheck.flagged_months} flagged month(s)</dd></> : null}
    <dt>Recommended</dt><dd className="wrap">{machine.recommended_action || '—'}</dd>
    {machine.augury_url ? <><dt>Augury</dt><dd><a href={machine.augury_url} target="_blank" rel="noreferrer">Open <ExternalLink size={12} /></a></dd></> : null}
  </dl>;
}
