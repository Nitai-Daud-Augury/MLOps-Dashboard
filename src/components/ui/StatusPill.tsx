import { AlertTriangle, CheckCircle2 } from 'lucide-react';

import type { BackfillStatus } from '../../types';

function labelForStatus(status: BackfillStatus) {
  return status.split('_').map((part) => part[0].toUpperCase() + part.slice(1)).join(' ');
}

export function StatusPill({ status }: { status: BackfillStatus }) {
  return <span className={`status-pill ${status}`}>
    {status === 'backfilled' || status === 'no_source_data' ? <CheckCircle2 size={14} /> : <AlertTriangle size={14} />}
    {labelForStatus(status)}
  </span>;
}
