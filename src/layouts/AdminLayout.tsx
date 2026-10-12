import { ShieldAlert } from 'lucide-react';
import { useStatus } from '../state/statusStore';
import { AppShell } from './AppShell';

/** Admin: amber accent + ADMIN band; every action here dispatches or stops real workflows. */
export function AdminLayout() {
  const { runtime } = useStatus();
  return <AppShell mode="admin"
    ribbon={<div className="admin-band" role="note"><ShieldAlert size={14} aria-hidden="true" /><strong>ADMIN</strong>
      <span>Actions here dispatch or stop real workflows{runtime ? ` · runtime ${runtime.mode}` : ''}{runtime?.workflow_backend ? ` · backend ${runtime.workflow_backend}` : ''}.</span></div>} />;
}
