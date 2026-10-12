import { AlertTriangle, Check, Copy, Info, Loader2, RefreshCw, X, XCircle } from 'lucide-react';
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { exactTime, relativeTime } from '../../lib/format';
import { TONE_ICON, type Tone } from '../../lib/status';


export function Spinner({ size = 14, label }: { size?: number; label?: string }) {
  return <Loader2 className="spin" size={size} aria-label={label} aria-hidden={label ? undefined : true} />;
}

export function StatusBadge({ tone, label, title }: { tone: Tone; label: string; title?: string }) {
  return <span className={`badge tone-${tone}`} title={title}><span aria-hidden="true" className="badge-icon">{TONE_ICON[tone]}</span>{label}</span>;
}

export function TestBadge() {
  return <span className="badge badge-test" title="Test machine">TEST</span>;
}

export function CopyIdButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  return <button type="button" className="icon-btn" title={copied ? 'Copied' : 'Copy ID'} aria-label={`Copy ${value}`}
    onClick={(event) => { event.stopPropagation(); void navigator.clipboard?.writeText(value).then(() => { setCopied(true); window.setTimeout(() => setCopied(false), 1200); }); }}>
    {copied ? <Check size={13} /> : <Copy size={13} />}
  </button>;
}

/** Relative time that re-renders every 15s; the exact time (Asia/Jerusalem) is in the tooltip. */
export function Timestamp({ value, prefix }: { value?: string | number | null; prefix?: string }) {
  const [, tick] = useState(0);
  useEffect(() => { const id = window.setInterval(() => tick((n) => n + 1), 15_000); return () => window.clearInterval(id); }, []);
  if (value === null || value === undefined || value === '') return <span className="muted">{prefix ? `${prefix} ` : ''}never</span>;
  return <time className="mono" dateTime={new Date(value).toISOString?.() ?? String(value)} title={`${exactTime(value)} (IL)`}>{prefix ? `${prefix} ` : ''}{relativeTime(value)}</time>;
}

type BannerTone = 'info' | 'warn' | 'error';
export function Banner({ tone, title, children, actions, onDismiss }: { tone: BannerTone; title?: ReactNode; children?: ReactNode; actions?: ReactNode; onDismiss?: () => void }) {
  const Icon = tone === 'error' ? XCircle : tone === 'warn' ? AlertTriangle : Info;
  return <section className={`banner banner-${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
    <Icon size={16} aria-hidden="true" />
    <div className="banner-body">{title ? <strong>{title}</strong> : null}{children ? <span>{children}</span> : null}</div>
    {actions ? <div className="banner-actions">{actions}</div> : null}
    {onDismiss ? <button type="button" className="icon-btn" aria-label="Dismiss" onClick={onDismiss}><X size={14} /></button> : null}
  </section>;
}

export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return <div className="empty"><strong>{title}</strong>{children ? <p>{children}</p> : null}{action}</div>;
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return <div className="empty empty-error" role="alert"><strong>Could not load</strong><p>{message}</p>
    {onRetry ? <button type="button" className="btn" onClick={onRetry}><RefreshCw size={14} />Retry</button> : null}</div>;
}

export function SkeletonRows({ columns, rows = 6 }: { columns: number; rows?: number }) {
  return <>{Array.from({ length: rows }, (_, row) => <tr key={row} className="skeleton-row" aria-hidden="true">
    {Array.from({ length: columns }, (__, col) => <td key={col}><span className="skeleton" /></td>)}</tr>)}</>;
}

export function KpiChip({ label, value, tone }: { label: string; value: ReactNode; tone?: Tone }) {
  return <div className={`kpi${tone ? ` tone-${tone}` : ''}`}><span className="kpi-value mono">{value}</span><span className="kpi-label">{label}</span></div>;
}

/**
 * Refresh with the server's rate limit: spinning + disabled while fetching, "Retry in Ns"
 * (aria-live) while the cooldown runs.
 */
export function RefreshButton({ onClick, fetching, retryIn = 0, label = 'Refresh' }: { onClick: () => void; fetching: boolean; retryIn?: number; label?: string }) {
  return <button type="button" className="btn" onClick={onClick} disabled={fetching || retryIn > 0} aria-live="polite"
    title={retryIn > 0 ? `Refresh is rate limited; available in ${retryIn}s` : label}>
    <RefreshCw size={14} className={fetching ? 'spin' : ''} aria-hidden="true" />
    {fetching ? 'Refreshing…' : retryIn > 0 ? `Retry in ${retryIn}s` : label}
  </button>;
}

/** Modal dialog on the native <dialog> (focus trap, Esc closes). `confirmText` turns on type-to-confirm. */
export function ConfirmDialog({ open, title, children, confirmLabel, danger, confirmText, busy, error, onConfirm, onCancel, extraActions }: {
  open: boolean; title: string; children?: ReactNode; confirmLabel: string; danger?: boolean; confirmText?: string;
  busy?: boolean; error?: string | null; onConfirm: () => void; onCancel: () => void; extraActions?: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [typed, setTyped] = useState('');
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) { setTyped(''); dialog.showModal(); }
    if (!open && dialog.open) dialog.close();
  }, [open]);
  const blocked = Boolean(confirmText) && typed !== confirmText;
  return <dialog ref={ref} className="dialog" aria-labelledby={titleId} onCancel={(event) => { event.preventDefault(); if (!busy) onCancel(); }}>
    <h2 id={titleId}>{title}</h2>
    <div className="dialog-body">{children}</div>
    {confirmText ? <label className="field"><span>Type <code>{confirmText}</code> to confirm</span>
      <input value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" spellCheck={false} /></label> : null}
    {error ? <p className="field-error" role="alert">{error}</p> : null}
    <div className="dialog-actions">
      <button type="button" className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
      {extraActions}
      <button type="button" className={`btn ${danger ? 'btn-danger' : 'btn-primary'}`} onClick={onConfirm} disabled={busy || blocked}>
        {busy ? <Spinner /> : null}{confirmLabel}</button>
    </div>
  </dialog>;
}
