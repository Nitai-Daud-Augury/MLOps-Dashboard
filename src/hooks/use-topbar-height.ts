import { useEffect } from 'react';

/**
 * Publish the real sticky header height as `--topbar-height` on <html>, so
 * fixed overlays (toasts) sit below the header at every width (the header
 * wraps to two rows under 1100px).
 */
export function useTopbarHeightVar(deps: unknown[] = []) {
  useEffect(() => {
    const header = document.querySelector<HTMLElement>('.topbar');
    const root = document.documentElement;
    if (!header) return;
    const update = () => root.style.setProperty('--topbar-height', `${Math.ceil(header.getBoundingClientRect().height)}px`);
    update();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', update);
      return () => window.removeEventListener('resize', update);
    }
    const observer = new ResizeObserver(update);
    observer.observe(header);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
