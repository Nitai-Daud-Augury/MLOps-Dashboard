import { useSearchParams } from 'react-router-dom';

export const PAGE_SIZES = [10, 25, 50, 100];

/** URL-backed paging + sort state; `prefix` keeps two tables on one page apart. */
export function useTableState(prefix = '', defaultSize = 25, defaultSort = '') {
  const [params, setParams] = useSearchParams();
  const size = PAGE_SIZES.includes(Number(params.get(`${prefix}size`))) ? Number(params.get(`${prefix}size`)) : defaultSize;
  const page = Math.max(1, Number(params.get(`${prefix}page`) ?? 1) || 1);
  const sort = params.get(`${prefix}sort`) ?? defaultSort;
  const dir: 1 | -1 = params.get(`${prefix}dir`) === 'desc' ? -1 : 1;
  const patch = (changes: Record<string, string | null>) => setParams((current) => {
    const next = new URLSearchParams(current);
    for (const [key, value] of Object.entries(changes)) { if (value === null || value === '') next.delete(prefix + key); else next.set(prefix + key, value); }
    return next;
  }, { replace: true });
  return {
    size, page, sort, dir,
    setPage: (value: number) => patch({ page: value <= 1 ? null : String(value) }),
    setSize: (value: number) => patch({ size: value === defaultSize ? null : String(value), page: null }),
    toggleSort: (key: string) => patch({ sort: key === defaultSort && sort === key && dir === -1 ? null : key, dir: sort === key && dir === 1 ? 'desc' : null, page: null }),
  };
}

