import { Loader2 } from 'lucide-react';

export function LoadingWidget({ label, compact = false }: { label: string; compact?: boolean }) {
  return <div className={`loading-widget ${compact ? 'compact' : ''}`} role="status" aria-live="polite">
    <Loader2 className="spin" size={compact ? 16 : 20} />
    <span>{label}</span><i className="loading-shimmer" />
  </div>;
}
