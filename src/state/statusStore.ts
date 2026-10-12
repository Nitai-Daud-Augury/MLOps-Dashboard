import { createContext, useContext } from 'react';
import type { RuntimeInfo, StatusPayload } from '../types';

export interface BlobSource { account: string; container: string }

export interface StatusContextValue {
  payload: StatusPayload | null;
  error: string | null;
  loading: boolean;
  refreshing: boolean;
  reload: () => Promise<unknown>;
  startScan: () => Promise<void>;
  resetScan: () => Promise<void>;
  scanRequesting: boolean;
  scanError: string | null;
  clearScanError: () => void;
  blobSource: BlobSource;
  setBlobSource: (source: BlobSource) => void;
  runtime: RuntimeInfo | null;
  health: 'ok' | 'down' | 'checking';
}

export const StatusContext = createContext<StatusContextValue | null>(null);

export function useStatus(): StatusContextValue {
  const value = useContext(StatusContext);
  if (!value) throw new Error('useStatus outside StatusProvider');
  return value;
}
