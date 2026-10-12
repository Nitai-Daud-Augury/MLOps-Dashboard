import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { EmptyState } from './components/ui';
import { ToastProvider } from './components/ui/Toasts';
import { AdminLayout } from './layouts/AdminLayout';
import { MonitorLayout } from './layouts/MonitorLayout';
import { AdminOverviewPage } from './pages/admin/AdminOverviewPage';
import { BackfillPage } from './pages/admin/BackfillPage';
import { DiagnosticsPage } from './pages/admin/DiagnosticsPage';
import { OperationsPage } from './pages/admin/OperationsPage';
import { FleetPage } from './pages/monitor/FleetPage';
import { LivePage } from './pages/monitor/LivePage';
import { MachinePage } from './pages/monitor/MachinePage';
import { SourceSettingsPage } from './pages/monitor/SourceSettingsPage';
import { StatusProvider } from './state/StatusContext';

/** Old links used /?view=detail&machine=<id>; send them to the machine page. */
function RootRedirect() {
  const params = new URLSearchParams(useLocation().search);
  const machine = params.get('machine');
  return <Navigate to={machine ? `/monitor/machines/${encodeURIComponent(machine)}` : '/monitor'} replace />;
}

function NotFound() {
  return <main className="content"><EmptyState title="Page not found" action={<a className="btn" href="/monitor">Go to Monitoring</a>}>This address does not match a dashboard route.</EmptyState></main>;
}

export default function App() {
  return <ToastProvider>
    <StatusProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<RootRedirect />} />
          <Route path="/monitor" element={<MonitorLayout />}>
            <Route index element={<FleetPage />} />
            <Route path="machines/:id" element={<MachinePage />} />
            <Route path="live" element={<LivePage />} />
            <Route path="settings/source" element={<SourceSettingsPage />} />
          </Route>
          <Route path="/admin" element={<AdminLayout />}>
            <Route index element={<AdminOverviewPage />} />
            <Route path="backfill" element={<BackfillPage />} />
            <Route path="operations" element={<OperationsPage />} />
            <Route path="diagnostics" element={<DiagnosticsPage />} />
          </Route>
          <Route path="*" element={<NotFound />} />
        </Routes>
      </BrowserRouter>
    </StatusProvider>
  </ToastProvider>;
}
