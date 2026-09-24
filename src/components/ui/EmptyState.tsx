import type { ReactNode } from 'react';
import { Database } from 'lucide-react';

export function EmptyState({ message = 'Run a scan to load ULRPM machine backfill status.', icon }: { message?: string; icon?: ReactNode }) {
  return <div className="empty-state">{icon ?? <Database size={32} />}<p>{message}</p></div>;
}
