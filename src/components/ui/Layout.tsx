import { Info } from 'lucide-react';
import type { ReactNode } from 'react';

/** Info icon with a tooltip; focusable so keyboard users get the text too. */
export function InfoTip({ text }: { text: string }) {
  return <span className="info" tabIndex={0} role="img" aria-label={text} title={text}><Info size={14} aria-hidden="true" /></span>;
}

/** Page title + one-line purpose (spec v2 §7) + right-aligned actions. */
export function PageHeader({ title, purpose, meta, actions }: { title: ReactNode; purpose: string; meta?: ReactNode; actions?: ReactNode }) {
  return <div className="page-head">
    <div className="page-head-text"><h1>{title}</h1><p className="muted">{purpose}</p>{meta ? <div className="meta">{meta}</div> : null}</div>
    {actions ? <div className="actions">{actions}</div> : null}
  </div>;
}

/** Card section: title, a sentence that says what it does, optional actions. */
export function Section({ title, description, actions, children, id, flush }: { title: ReactNode; description: string; actions?: ReactNode; children: ReactNode; id?: string; flush?: boolean }) {
  return <section className={flush ? 'card' : 'card section'} aria-labelledby={id}>
    <div className="section-head" style={flush ? { padding: '20px 24px 0' } : undefined}>
      <div><h2 id={id}>{title}</h2><p className="helper">{description}</p></div>
      {actions ? <div className="actions">{actions}</div> : null}
    </div>
    {children}
  </section>;
}

export type KpiTone = 'ok' | 'warn' | 'danger' | 'primary' | 'violet' | 'admin' | 'neutral';
export type KpiState = 'ready' | 'loading' | 'scanning' | 'stale' | 'error';

/**
 * Big KPI card: icon tile, value, label with tooltip, helper line. Clicking applies a filter
 * (aria-pressed); states: loading (skeleton), scanning (value dimmed), stale (amber dot), error (—).
 */
export function KpiCard({ icon, label, value, helper, tooltip, tone = 'neutral', state = 'ready', active, onClick, size }: {
  icon: ReactNode; label: string; value: ReactNode; helper?: ReactNode; tooltip?: string; tone?: KpiTone; state?: KpiState;
  active?: boolean; onClick?: () => void; size?: 'sm';
}) {
  const body = <>
    <span className="kpi-icon" aria-hidden="true">{icon}</span>
    <span className="kpi-label">{label}{tooltip ? <InfoTip text={tooltip} /> : null}</span>
    <span className="kpi-value">{state === 'loading' ? <span className="skel-value" aria-label="Loading" /> : state === 'error' ? '—' : value}</span>
    {helper ? <span className="kpi-helper">{state === 'error' ? 'Could not load' : helper}</span> : null}
    {state === 'stale' ? <span className="stale-dot" title="Showing cached data; a refresh is pending or failed" /> : null}
  </>;
  const className = `kpi t-${tone}${active ? ' active' : ''}${state === 'scanning' ? ' dim' : ''}${size ? ` ${size}` : ''}`;
  return onClick
    ? <button type="button" className={className} aria-pressed={Boolean(active)} onClick={onClick} title={active ? 'Click to clear this filter' : 'Click to filter the table'}>{body}</button>
    : <div className={className}>{body}</div>;
}

/** Coverage meter: bar + "n/m" text (never color alone). */
export function Meter({ done, total }: { done: number; total: number }) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  return <span className="meter" title={`${pct}% of expected months present`}>
    <span className="meter-track" aria-hidden="true"><span className="meter-fill" style={{ width: `${pct}%`, display: 'block', background: pct === 100 ? 'var(--ok)' : pct >= 50 ? 'var(--warn)' : 'var(--danger)' }} /></span>
    <span className="mono">{done}/{total}</span>
  </span>;
}
