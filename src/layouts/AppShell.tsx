import { Activity, ChevronsLeft, ChevronsRight, ClipboardList, Database, Gauge, LayoutGrid, Menu, Moon, PlayCircle, ShieldAlert, Stethoscope, Sun, Waves } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { SOURCE_NAMES } from '../components/features-source/featuresSourceApi';
import { relativeTime } from '../lib/format';
import { isFinishedWorkflow } from '../lib/status';
import { useStatus } from '../state/statusStore';
import type { WorkflowSummaryPayload } from '../types';
import { readCachedJson } from '../utils/cache';

function useTheme(): [boolean, () => void] {
  const [dark, setDark] = useState(() => localStorage.getItem('ulrpm-theme') === 'dark');
  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark);
    localStorage.setItem('ulrpm-theme', dark ? 'dark' : 'light');
  }, [dark]);
  return [dark, () => setDark((value) => !value)];
}

function HealthDot() {
  const { health, runtime } = useStatus();
  const label = health === 'ok' ? `API ok${runtime ? ` · ${runtime.mode}` : ''}` : health === 'down' ? 'API unreachable' : 'Checking API…';
  return <span className={`health health-${health}`} role="status" title={label}><span className="dot" aria-hidden="true" /><span className="only-desktop">{label}</span></span>;
}

/** Live counts for the nav badges, read from the cached workflow summary only (no extra request). */
function useNavCounts(): { active: number; pending: number } {
  const location = useLocation();
  const [counts, setCounts] = useState({ active: 0, pending: 0 });
  useEffect(() => {
    const read = () => {
      const data = readCachedJson<WorkflowSummaryPayload>('admin-workflow-summary', 24 * 3600_000);
      const workflows = data?.workflows ?? [];
      setCounts({ active: workflows.filter((w) => w.pending || !isFinishedWorkflow(w.status)).length, pending: data?.pending_count ?? workflows.filter((w) => w.pending).length });
    };
    read();
    const id = window.setInterval(read, 10_000);
    return () => window.clearInterval(id);
  }, [location.pathname]);
  return counts;
}

const CRUMBS: [RegExp, string][] = [
  [/^\/monitor\/machines\//, 'Machine'], [/^\/monitor\/live/, 'Live Ops'], [/^\/monitor\/(source|settings\/source)/, 'Data source'], [/^\/monitor/, 'Fleet coverage'],
  [/^\/admin\/backfill/, 'Backfill'], [/^\/admin\/operations/, 'Operations'], [/^\/admin\/diagnostics/, 'Diagnostics'], [/^\/admin/, 'Overview'],
];

function NavItem({ to, icon, label, end, admin, badge, onNavigate }: { to: string; icon: ReactNode; label: string; end?: boolean; admin?: boolean; badge?: number; onNavigate: () => void }) {
  return <NavLink to={to} end={end} onClick={onNavigate} title={label} className={({ isActive }) => `nav-item${admin ? ' admin' : ''}${isActive ? ' active' : ''}`}>
    {icon}<span className="nav-label">{label}</span>{badge ? <span className="nav-badge" aria-label={`${badge} active`}>{badge}</span> : null}
  </NavLink>;
}

/**
 * Shared chrome: collapsible sidebar (256/64px, persisted; off-canvas below 1024px) with the
 * MONITORING and ADMIN groups, a breadcrumb top bar, and the amber band on admin routes.
 * `mode` drives the accent via data-mode.
 */
export function AppShell({ mode, headerExtra, ribbon, banners }: { mode: 'monitor' | 'admin'; headerExtra?: ReactNode; ribbon?: ReactNode; banners?: ReactNode }) {
  const [dark, toggleTheme] = useTheme();
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('ulrpm-sidebar') === 'collapsed');
  const [mobileOpen, setMobileOpen] = useState(false);
  const { payload } = useStatus();
  const location = useLocation();
  const counts = useNavCounts();
  useEffect(() => { localStorage.setItem('ulrpm-sidebar', collapsed ? 'collapsed' : 'expanded'); }, [collapsed]);
  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') setMobileOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [mobileOpen]);
  const crumb = CRUMBS.find(([re]) => re.test(location.pathname))?.[1] ?? '';
  const source = payload?.data_sources?.features_source?.active_source;
  const generated = payload?.snapshot.machines.length ? payload.snapshot.generated_at : null;
  const close = () => setMobileOpen(false);
  return <div className={`shell${collapsed ? ' collapsed' : ''}${mobileOpen ? ' mobile-open' : ''}`} data-mode={mode}>
    <aside className="sidebar" aria-label="Primary">
      <Link to="/monitor" className="brand" style={{ color: 'inherit', textDecoration: 'none' }} onClick={close}>
        <span className="brand-mark"><Waves size={16} /></span><span className="brand-name">Drift Dashboard</span></Link>
      <nav aria-label="Monitoring" style={{ display: 'grid', gap: 2 }}>
        <div className="nav-group"><span>Monitoring</span></div>
        <NavItem to="/monitor" end icon={<LayoutGrid size={18} />} label="Fleet coverage" onNavigate={close} />
        <NavItem to="/monitor/live" icon={<Activity size={18} />} label="Live Ops" badge={counts.active} onNavigate={close} />
        <NavItem to="/monitor/source" icon={<Database size={18} />} label="Data source" onNavigate={close} />
      </nav>
      <nav aria-label="Admin" style={{ display: 'grid', gap: 2 }}>
        <div className="nav-group admin"><ShieldAlert size={12} aria-hidden="true" /><span>Admin</span></div>
        <NavItem admin to="/admin" end icon={<Gauge size={18} />} label="Overview" onNavigate={close} />
        <NavItem admin to="/admin/backfill" icon={<PlayCircle size={18} />} label="Backfill" onNavigate={close} />
        <NavItem admin to="/admin/operations" icon={<ClipboardList size={18} />} label="Operations" badge={counts.pending} onNavigate={close} />
        <NavItem admin to="/admin/diagnostics" icon={<Stethoscope size={18} />} label="Diagnostics" onNavigate={close} />
      </nav>
      <div className="sidebar-foot">
        <span>Source: <strong>{source ? SOURCE_NAMES[source] : '—'}</strong></span>
        <span title="Age of the coverage scan">Scan: {generated ? relativeTime(generated) : 'none yet'}</span>
      </div>
      <button type="button" className="nav-item only-desktop" style={{ border: 0, background: 'transparent', cursor: 'pointer', font: 'inherit' }}
        onClick={() => setCollapsed((value) => !value)} aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'} title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}>
        {collapsed ? <ChevronsRight size={18} /> : <ChevronsLeft size={18} />}<span className="nav-label">Collapse</span></button>
    </aside>
    <div className="scrim" onClick={close} aria-hidden="true" />
    <div className="main">
      <header className="topbar">
        <button type="button" className="icon-btn only-mobile" aria-label="Open navigation" onClick={() => setMobileOpen(true)}><Menu size={18} /></button>
        <nav className="crumbs" aria-label="Breadcrumb"><span>{mode === 'admin' ? 'Admin' : 'Monitoring'}</span><span aria-hidden="true">/</span><strong>{crumb}</strong></nav>
        <span className="grow" />
        {headerExtra}
        <HealthDot />
        <button type="button" className="icon-btn" onClick={toggleTheme} aria-label={dark ? 'Light theme' : 'Dark theme'} title={dark ? 'Light theme' : 'Dark theme'}>
          {dark ? <Sun size={16} /> : <Moon size={16} />}</button>
      </header>
      {ribbon}
      <main className="content">
        {banners ? <div className="banner-stack">{banners}</div> : null}
        <Outlet />
      </main>
    </div>
  </div>;
}
