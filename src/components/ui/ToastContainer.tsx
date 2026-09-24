import { Check, X, Zap } from 'lucide-react';

import type { Toast } from '../../types';

export function ToastContainer({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: string) => void }) {
  return <div className="toast-container">{toasts.map((toast) => (
    <div className={`toast toast-${toast.type}${toast.exiting ? ' exiting' : ''}`} key={toast.id}>
      {toast.type === 'success' ? <Check size={16} /> : toast.type === 'error' ? <X size={16} /> : <Zap size={16} />}
      <span>{toast.message}</span>
      <button className="toast-close" type="button" onClick={() => onDismiss(toast.id)} aria-label="Dismiss"><X size={14} /></button>
    </div>
  ))}</div>;
}
