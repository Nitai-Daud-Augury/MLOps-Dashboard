import { useEffect, useState } from 'react';
import { inventoryPage } from './api';
import type { InventoryFilters, InventoryPage } from './types';

export function useInventory(filters: InventoryFilters, cursor: string | null) {
  const [page, setPage] = useState<InventoryPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setLoading(true);
      inventoryPage(filters, cursor, controller.signal).then((value) => { setPage(value); setError(''); })
        .catch((value: unknown) => { if (!controller.signal.aborted) setError(value instanceof Error ? value.message : String(value)); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [filters, cursor]);
  return { page, loading, error };
}
