import { createContext, useContext } from 'react';

export type ToastType = 'success' | 'error' | 'info';
export type Notify = (type: ToastType, message: string) => void;
export const ToastContext = createContext<Notify>(() => undefined);
export const useToast = () => useContext(ToastContext);
