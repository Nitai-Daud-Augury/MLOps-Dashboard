import { useCallback, useState } from 'react';

import { TOAST_DURATION_MS } from '../constants';
import type { Toast } from '../types';

export function useToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.map((toast) => toast.id === id ? { ...toast, exiting: true } : toast));
    window.setTimeout(() => setToasts((prev) => prev.filter((toast) => toast.id !== id)), 200);
  }, []);
  const add = useCallback((type: Toast['type'], message: string) => {
    const toast: Toast = { id: crypto.randomUUID(), type, message, timestamp: Date.now() };
    setToasts((prev) => [...prev, toast]);
    setTimeout(() => dismiss(toast.id), TOAST_DURATION_MS);
  }, [dismiss]);
  return { toasts, add, dismiss };
}
