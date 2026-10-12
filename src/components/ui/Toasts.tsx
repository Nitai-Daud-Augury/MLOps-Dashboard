import { X } from 'lucide-react';
import { useCallback, useMemo, useRef, useState, type ReactNode } from 'react';
import { ToastContext, type Notify, type ToastType } from './toastContext';

interface ToastItem { id: number; type: ToastType; message: string }

/** Toast stack (bottom-right, below nothing, never over the header). Errors stay until dismissed. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const next = useRef(1);
  const dismiss = useCallback((id: number) => setItems((current) => current.filter((item) => item.id !== id)), []);
  const notify = useCallback<Notify>((type, message) => {
    const id = next.current++;
    setItems((current) => [...current.slice(-4), { id, type, message }]);
    if (type !== 'error') window.setTimeout(() => dismiss(id), 5000);
  }, [dismiss]);
  const value = useMemo(() => notify, [notify]);
  return <ToastContext.Provider value={value}>
    {children}
    <div className="toasts" aria-live="polite">
      {items.map((item) => (
        <div key={item.id} className={`toast toast-${item.type}`} role={item.type === 'error' ? 'alert' : 'status'}>
          <span>{item.message}</span>
          <button type="button" className="icon-btn" aria-label="Dismiss notification" onClick={() => dismiss(item.id)}><X size={14} /></button>
        </div>
      ))}
    </div>
  </ToastContext.Provider>;
}
