import { X } from 'lucide-react';
import { useEffect, useId, useRef, type ReactNode } from 'react';

export interface QuickViewField { label: string; value: ReactNode }

/** Shared centered modal (native <dialog>: focus trap, Esc/backdrop close) showing label/value metadata. */
export function QuickView({ open, title, subtitle, fields, actions, onClose }: {
  open: boolean; title: ReactNode; subtitle?: ReactNode; fields: QuickViewField[]; actions?: ReactNode; onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return <dialog ref={ref} className="dialog quick-view" aria-labelledby={titleId}
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="quick-view-head">
      <div><h2 id={titleId}>{title}</h2>{subtitle ? <p className="helper">{subtitle}</p> : null}</div>
      <button type="button" className="btn btn-ghost btn-sm" aria-label="Close" onClick={onClose}><X size={16} /></button>
    </div>
    <dl className="quick-view-fields">{fields.map((f) => <div key={f.label} className="quick-view-row"><dt>{f.label}</dt><dd className="wrap">{f.value}</dd></div>)}</dl>
    {actions ? <div className="dialog-actions">{actions}</div> : null}
  </dialog>;
}
