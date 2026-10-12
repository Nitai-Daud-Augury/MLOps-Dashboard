import { ShieldAlert } from 'lucide-react';
import { useStatus } from '../state/statusStore';
import { AppShell } from './AppShell';

const NAV = [
  { to: '/admin', label: 'Overview', end: true },
  { to: '/admin/backfill', label: 'Backfill' },
  { to: '/admin/operations', label: 'Operations' },
  { to: '/admin/diagnostics', label: 'Diagnostics' },
];

/** Admin: amber chrome + ADMIN ribbon; every action here dispatches or stops real workflows. */
export function AdminLayout() {
  const { runtime } = useStatus();
  return <AppShell mode="admin" nav={NAV}
    ribbon={<div className="admin-ribbon" role="note"><ShieldAlert size={14} aria-hidden="true" /><strong>ADMIN</strong>
      <span>Actions here dispatch real workflows{runtime ? ` · runtime ${runtime.mode}` : ''}{runtime?.workflow_backend ? ` · backend ${runtime.workflow_backend}` : ''}.</span></div>} />;
}
