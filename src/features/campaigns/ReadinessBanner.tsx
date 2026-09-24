import { useEffect, useState } from 'react';
import { AlertTriangle, ShieldCheck } from 'lucide-react';
import { getControlStatus } from './api';
import type { ControlStatus } from './types';

export function ReadinessBanner() {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  useEffect(() => { void getControlStatus().then(setStatus).catch(() => setStatus(null)); }, []);
  if (!status) return <section className="control-readiness waiting">Checking control-plane readiness…</section>;
  return <section className={`control-readiness ${status.production_ready ? 'ready' : 'blocked'}`}>
    {status.production_ready ? <ShieldCheck size={18} /> : <AlertTriangle size={18} />}
    <div><strong>{status.production_ready ? 'Production controls verified' : 'Production remains locked'}</strong>
      <p>Inventory: {status.inventory.status} ({status.inventory.source}) · dispatch {status.dispatch_enabled ? 'enabled' : 'disabled'}</p>
      {status.blockers.length ? <small>{status.blockers.join(' · ')}</small> : null}</div>
  </section>;
}
