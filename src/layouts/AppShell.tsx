import { Moon, Sun } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useStatus } from '../state/statusStore';

export interface NavItem { to: string; label: string; end?: boolean }

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
  return <span className={`health health-${health}`} role="status" title={label}><span className="dot" aria-hidden="true" />{label}</span>;
}

/** Shared chrome. `mode` drives the accent (monitor = blue, admin = amber) via data-mode. */
export function AppShell({ mode, nav, headerExtra, ribbon, banners }: { mode: 'monitor' | 'admin'; nav: NavItem[]; headerExtra?: ReactNode; ribbon?: ReactNode; banners?: ReactNode }) {
  const [dark, toggleTheme] = useTheme();
  return <div className="shell" data-mode={mode}>
    <header className="topbar">
      <div className="topbar-row">
        <span className="brand">Drift Dashboard</span>
        <nav className="mode-switch" aria-label="Mode">
          <NavLink to="/monitor" className={({ isActive }) => `mode-link${isActive ? ' on' : ''}`}>Monitoring</NavLink>
          <NavLink to="/admin" className={({ isActive }) => `mode-link mode-admin${isActive ? ' on' : ''}`}>Admin</NavLink>
        </nav>
        <span className="grow" />
        {headerExtra}
        <HealthDot />
        <button type="button" className="icon-btn" onClick={toggleTheme} aria-label={dark ? 'Light theme' : 'Dark theme'} title={dark ? 'Light theme' : 'Dark theme'}>
          {dark ? <Sun size={15} /> : <Moon size={15} />}</button>
      </div>
      {ribbon}
      <nav className="subnav" aria-label={mode === 'admin' ? 'Admin sections' : 'Monitoring sections'}>
        {nav.map((item) => <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => `subnav-link${isActive ? ' on' : ''}`}>{item.label}</NavLink>)}
      </nav>
    </header>
    <main className="content">
      {banners ? <div className="banner-stack">{banners}</div> : null}
      <Outlet />
    </main>
  </div>;
}
