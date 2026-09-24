import type { ActiveMonth, AdminAction, AdminReadiness, AdminSpec, AdminView, AllMachinesBackfillForm, BackfillScope, BackfillStatus, BusyMachineInfo, DashboardSnapshot, DashboardSummary, EnvironmentTarget, FullRlblTestForm, LogsForm, MachineManifestForm, MachineStatus, MachineView, ManifestResult, MonthStatus, MultiMachineManifestForm, RunningWorkflow, RunningWorkflowsPayload, RuntimeInfo, SandboxEvent, SourceType, StatusPayload, StopForm, Tab, Toast, TriggerForm, WorkflowSourceForm, WorkflowSummaryPayload } from './types';
import { API_BASE, BLOB_SOURCE_STORAGE_KEY, DEFAULT_DEV_NAMESPACE, DEFAULT_OUTERBOUNDS_URL, EMPTY_MACHINES, OUTERBOUNDS_BASE, PROD_ACCOUNT, PROD_CONFIRMATION, PROD_CONTAINER, RECOMMENDED_MACHINE_CONCURRENCY, STATUS_OPTIONS, TEST_ACCOUNT, TEST_CONTAINER } from './constants';
import { backfillMonthUntil, formatElapsedTime, formatNumber, getCachedJson, orchestratorMonthIndex, orchestratorMonthsThroughToday, utcToday, workflowSocketUrl, writeCachedJson } from './utils';
import { useToasts } from './hooks';
import { CopyMachineIdButton, CurrentCostPanel, EmptyState, LoadingMetrics, LoadingWidget, LogViewer, ManifestCreatedDialog, Progress, StatusPill, ToastContainer } from './components';
import { MachineFeatureChart } from './features/machine-feature-chart/MachineFeatureChart';
import { CampaignWorkspace } from './features/campaigns';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  AlertTriangle,
  Activity,
  Check,
  CheckCircle2,
  CalendarDays,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  ExternalLink,
  Eye,
  FileCode2,
  Filter,
  FlaskConical,
  History,
  Loader2,
  Play,
  RefreshCw,
  Search,
  ShieldAlert,
  Moon,
  Sun,
  Square,
  Terminal,
  UploadCloud,
  X,
  Zap,
} from 'lucide-react';
import './App.css';

function TestMachineBadge({ isTest }: { isTest?: boolean }) {
  return isTest ? <span className="test-machine-badge" title="Test machine; exclude from real-machine reporting">Test</span> : null;
}




function App() {
  const [activeTab, setActiveTab] = useState<Tab>('monitor');
  const [tabDirection, setTabDirection] = useState<'left' | 'right'>('right');
  const [payload, setPayload] = useState<StatusPayload | null>(null);
  const [runtimeInfo, setRuntimeInfo] = useState<RuntimeInfo | null>(null);
  const [statusFilter, setStatusFilter] = useState<(typeof STATUS_OPTIONS)[number]>('all');
  const [query, setQuery] = useState('');
  const [sourceAccount, setSourceAccount] = useState(() => getSavedBlobSource().account);
  const [sourceContainer, setSourceContainer] = useState(() => getSavedBlobSource().container);
  const [darkMode, setDarkMode] = useState(() => localStorage.getItem('ulrpm-theme') === 'dark');
  const [apiError, setApiError] = useState<string | null>(null);
  const [statusRefreshing, setStatusRefreshing] = useState(false);
  const { toasts, add: addToast, dismiss: dismissToast } = useToasts();
  const handleTabChange = (tab: Tab) => {
    const tabOrder: Tab[] = ['monitor', 'admin', 'sandbox'];
    setTabDirection(tabOrder.indexOf(tab) > tabOrder.indexOf(activeTab) ? 'right' : 'left');
    setActiveTab(tab);
  };

  const loadStatus = useCallback(async (force = false) => {
    setStatusRefreshing(true);
    try {
      setPayload(await getCachedJson<StatusPayload>('backfill-status', `${API_BASE}/api/backfill/status`, 5 * 60_000, force));
      setApiError(null);
    } catch (error) {
      setApiError(error instanceof Error ? error.message : String(error));
    } finally {
      setStatusRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    fetch(`${API_BASE}/api/health`, { cache: 'no-store' })
      .then((response) => response.ok ? response.json() as Promise<{ runtime?: RuntimeInfo }> : Promise.reject(new Error('Runtime capability check failed')))
      .then((result) => setRuntimeInfo(result.runtime ?? null))
      .catch(() => setRuntimeInfo(null));
  }, []);

  const workflowMutationsEnabled = runtimeInfo?.capabilities.workflow_mutations === true;

  useEffect(() => {
    if (!payload?.scan_state.running) return;
    const interval = window.setInterval(() => void loadStatus(true), 5_000);
    return () => window.clearInterval(interval);
  }, [payload?.scan_state.running, loadStatus]);

  useEffect(() => {
    localStorage.setItem('ulrpm-theme', darkMode ? 'dark' : 'light');
  }, [darkMode]);

  useEffect(() => {
    const summary = payload?.snapshot.summary;
    if (!summary) { document.title = 'MLOps Dashboard'; return; }
    const errors = summary.partitions_with_scan_errors ?? 0;
    const gaps = summary.needs_backfill ?? 0;
    const prefix = errors > 0 ? '⚠ ' : gaps > 0 ? '● ' : '✓ ';
    document.title = `${prefix}MLOps Dashboard — ${coveragePercent(summary)} coverage`;
  }, [payload]);

  useEffect(() => {
    localStorage.setItem(
      BLOB_SOURCE_STORAGE_KEY,
      JSON.stringify({ account: sourceAccount, container: sourceContainer }),
    );
  }, [sourceAccount, sourceContainer]);

  const startScan = async () => {
    try {
      addToast('info', 'Starting blob scan...');
      const response = await fetch(`${API_BASE}/api/backfill/scan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_account: sourceAccount,
          source_container: sourceContainer,
        }),
      });
      if (!response.ok) {
        throw new Error(`Scan request failed: ${response.status}`);
      }
      const data = await response.json();
      if (data.already_running) {
        addToast('info', 'A scan is already running. Wait for it to finish or reset it.');
      } else {
        addToast('success', 'Scan started successfully.');
      }
      await loadStatus(true);
      setApiError(null);
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      setApiError(msg);
      addToast('error', `Scan failed: ${msg}`);
    }
  };

  const resetScan = async () => {
    try {
      const response = await fetch(`${API_BASE}/api/backfill/scan/reset`, { method: 'POST' });
      if (!response.ok) throw new Error(`Reset failed: ${response.status}`);
      addToast('success', 'Scan state reset. You can start a new scan now.');
      await loadStatus(true);
      setApiError(null);
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      setApiError(msg);
      addToast('error', `Reset failed: ${msg}`);
    }
  };

  const machines = payload?.snapshot.machines ?? EMPTY_MACHINES;
  const filteredMachines = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return machines.filter((machine) => {
      const matchesStatus = statusFilter === 'all' || machine.status === statusFilter;
      const matchesQuery = !needle || machine.machine_id.toLowerCase().includes(needle);
      return matchesStatus && matchesQuery;
    });
  }, [machines, query, statusFilter]);

  const detailMachineId = useMemo(
    () => new URLSearchParams(window.location.search).get('machine'),
    [],
  );
  const detailMachine = detailMachineId
    ? machines.find((machine) => machine.machine_id === detailMachineId) ?? null
    : null;
  const quickMachineId = useMemo(
    () => new URLSearchParams(window.location.search).get('quick'),
    [],
  );
  const quickMachine = quickMachineId
    ? machines.find((machine) => machine.machine_id === quickMachineId) ?? null
    : null;

  if (detailMachineId) {
    return (
      <><ToastContainer toasts={toasts} onDismiss={dismissToast} /><MachineDetailPage
        payload={payload}
        machine={detailMachine}
        machineId={detailMachineId}
        apiError={apiError}
        onRefresh={() => loadStatus(true)}
        isRefreshing={statusRefreshing}
        darkMode={darkMode}
        onToggleTheme={() => setDarkMode((current) => !current)}
        onToast={addToast}
        workflowMutationsEnabled={workflowMutationsEnabled}
      /></>
    );
  }

  if (quickMachineId) {
    return <><ToastContainer toasts={toasts} onDismiss={dismissToast} /><MachineQuickViewPage machine={quickMachine} machineId={quickMachineId} darkMode={darkMode} onToggleTheme={() => setDarkMode((current) => !current)} /></>;
  }

  return (
    <main className={`dashboard-shell ${darkMode ? 'dark' : ''}${activeTab === 'admin' ? ' admin-shell' : ''}`}>
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <p className="eyebrow">Production Feature Store</p>
            <h1>MLOps Dashboard</h1>
          </div>
          <div className="tabbar" role="tablist">
          <button className={activeTab === 'monitor' ? 'active' : ''} type="button" onClick={() => handleTabChange('monitor')}>
            <Database size={16} />
            <span>Monitor</span>
          </button>
          <button className={activeTab === 'admin' ? 'active' : ''} type="button" onClick={() => handleTabChange('admin')}>
            <Terminal size={16} />
            <span>Admin Actions</span>
          </button>
          <button className={activeTab === 'sandbox' ? 'active' : ''} type="button" onClick={() => handleTabChange('sandbox')}>
            <FlaskConical size={16} />
            <span>Sandbox</span>
          </button>
          </div>
          <button
          className="icon-button theme-toggle"
          type="button"
          onClick={() => setDarkMode((current) => !current)}
          title={darkMode ? 'Use light theme' : 'Use dark theme'}
        >
          {darkMode ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>
      </header>

      {apiError ? (
        <section className="notice error animate-shake">
          <ShieldAlert size={18} />
          <span>{apiError}. Start the backend at {API_BASE}.</span>
        </section>
      ) : null}

      {runtimeInfo && !workflowMutationsEnabled ? <section className="notice inline-notice runtime-mode-notice" role="status"><ShieldAlert size={17} /><span><strong>Monitor-only mode ({runtimeInfo.mode}).</strong> Workflow submissions, stops, and CLI log lookups are disabled here; monitoring and standalone manifest creation remain available.</span></section> : null}

      <div className={`tab-content from-${tabDirection}`}>
        <p className="subtitle">Monitor coverage and operate FST backfill workflows from one place.</p>
        {activeTab === 'monitor' ? (
          <MonitorTab
            payload={payload}
            filteredMachines={filteredMachines}
            statusFilter={statusFilter}
            query={query}
            sourceAccount={sourceAccount}
            sourceContainer={sourceContainer}
            onRefresh={() => loadStatus(true)}
            isRefreshing={statusRefreshing}
            onStartScan={startScan}
            onResetScan={resetScan}
            onStatusFilter={setStatusFilter}
            onQuery={setQuery}
            onSourceAccount={setSourceAccount}
            onSourceContainer={setSourceContainer}
            onToast={addToast}
            workflowMutationsEnabled={workflowMutationsEnabled}
          />
        ) : (
          activeTab === 'admin' ? <AdminTab onToast={addToast} workflowMutationsEnabled={workflowMutationsEnabled} /> : <SandboxTab />
        )}
      </div>
    </main>
  );
}

function MonitorTab({
  payload,
  filteredMachines,
  statusFilter,
  query,
  sourceAccount,
  sourceContainer,
  onRefresh,
  isRefreshing,
  onStartScan,
  onResetScan,
  onStatusFilter,
  onQuery,
  onSourceAccount,
  onSourceContainer,
  onToast,
  workflowMutationsEnabled,
}: {
  payload: StatusPayload | null;
  filteredMachines: MachineStatus[];
  statusFilter: (typeof STATUS_OPTIONS)[number];
  query: string;
  sourceAccount: string;
  sourceContainer: string;
  onRefresh: () => Promise<void>;
  isRefreshing: boolean;
  onStartScan: () => Promise<void>;
  onResetScan: () => Promise<void>;
  onStatusFilter: (value: (typeof STATUS_OPTIONS)[number]) => void;
  onQuery: (value: string) => void;
  onSourceAccount: (value: string) => void;
  onSourceContainer: (value: string) => void;
  onToast: (type: Toast['type'], message: string) => void;
  workflowMutationsEnabled: boolean;
}) {
  const summary = payload?.snapshot.summary;
  const warnings = payload?.snapshot.warnings ?? [];
  const scanRunning = payload?.scan_state.running ?? false;
  const scanStartedAt = payload?.scan_state.started_at;
  const scanProgress = payload?.scan_state;
  const machineScanPercent = scanProgress?.total_machines
    ? Math.round((scanProgress.completed_machines / scanProgress.total_machines) * 100)
    : 0;
  const [scanElapsed, setScanElapsed] = useState(0);
  const [messagesOpen, setMessagesOpen] = useState(false);
  const dashboardLoading = payload === null;
  const [accounts, setAccounts] = useState<string[]>([PROD_ACCOUNT, TEST_ACCOUNT]);
  const [containers, setContainers] = useState<string[]>([PROD_CONTAINER, TEST_CONTAINER]);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [quickMachine, setQuickMachine] = useState<MachineStatus | null>(null);
  const [filterPulse, setFilterPulse] = useState(false);
  const [activeBackfillCount, setActiveBackfillCount] = useState(0);
  const activeByMachine: Record<string, ActiveMonth[]> = {};
  const PAGE_SIZE = 15;

  useEffect(() => {
    if (!scanRunning || !scanStartedAt) { setScanElapsed(0); return; }
    const start = new Date(scanStartedAt).getTime();
    const tick = () => setScanElapsed(Math.floor((Date.now() - start) / 1000));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [scanRunning, scanStartedAt]);

  useEffect(() => {
    setCurrentPage(1);
    setFilterPulse(true);
    const timer = window.setTimeout(() => setFilterPulse(false), 300);
    return () => window.clearTimeout(timer);
  }, [query, statusFilter]);

  useEffect(() => {
    getCachedJson<{ accounts: string[] }>('blob-accounts', `${API_BASE}/api/blob-sources/accounts`, 60 * 60_000)
      .then((result) => setAccounts(result.accounts))
      .catch((error) => setSourceError(error instanceof Error ? error.message : String(error)));
  }, []);

  useEffect(() => {
    if (!sourceAccount.trim()) return;
    getCachedJson<{ containers?: string[] }>(`blob-containers:${sourceAccount.trim()}`, `${API_BASE}/api/blob-sources/containers?account=${encodeURIComponent(sourceAccount.trim())}`, 60 * 60_000)
      .then((result) => result.containers ?? [])
      .then((result) => {
        setContainers(result);
        setSourceError(null);
      })
      .catch((error) => setSourceError(error instanceof Error ? error.message : String(error)));
  }, [sourceAccount]);

  useEffect(() => {
    fetch(`${API_BASE}/api/backfill/runner-lock-status`)
      .then((response) => response.ok ? response.json() as Promise<{ busy_machines?: Record<string, unknown> }> : { busy_machines: {} })
      .then((data) => setActiveBackfillCount(Object.keys(data.busy_machines ?? {}).length))
      .catch(() => setActiveBackfillCount(0));
  }, [payload]);

  const totalPages = Math.max(1, Math.ceil(filteredMachines.length / PAGE_SIZE));
  const paginatedMachines = filteredMachines.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const healthLevel = !summary ? 'loading'
    : (summary.partitions_with_scan_errors ?? 0) > 0 ? 'error'
    : (summary.missing_partitions ?? 0) > 0 ? 'warning'
    : 'healthy';
  const healthHeadline = !summary ? 'Loading dashboard...'
    : healthLevel === 'error' ? `${summary.partitions_with_scan_errors} scan errors detected`
    : healthLevel === 'warning' ? `${summary.needs_backfill} machine${summary.needs_backfill === 1 ? '' : 's'} need attention`
    : 'All systems operational';
  const healthDetail = !summary ? 'Fetching latest backfill status'
    : `${formatNumber(summary.machine_count)} ULRPM machines monitored · ${coveragePercent(summary)} verified coverage · ${formatNumber(summary.production_rows)} FST rows`;
  useEffect(() => {
    if (warnings.length === 0) setMessagesOpen(false);
  }, [warnings.length]);

  return (
    <>
      {payload?.scan_state.error ? (
        <section className="notice error">
          <AlertTriangle size={18} />
          <span>{payload.scan_state.error}</span>
        </section>
      ) : null}

      {warnings.length > 0 ? (
        <section className={`dashboard-messages${messagesOpen ? ' open' : ''}`}>
          <button
            className="dashboard-messages-toggle"
            type="button"
            aria-expanded={messagesOpen}
            aria-controls="dashboard-warning-list"
            onClick={() => setMessagesOpen((open) => !open)}
          >
            <AlertTriangle size={17} aria-hidden="true" />
            <span>Messages</span>
            <span className="dashboard-message-count" aria-label={`${warnings.length} ${warnings.length === 1 ? 'message' : 'messages'}`}>
              {warnings.length}
            </span>
            <span className="dashboard-messages-hint">{messagesOpen ? 'Hide messages' : 'Show messages'}</span>
            <ChevronDown className="dashboard-messages-chevron" size={16} aria-hidden="true" />
          </button>
          <div id="dashboard-warning-list" className="dashboard-message-list" hidden={!messagesOpen}>
            <ul>
              {warnings.map((warning, index) => (
                <li key={`${index}-${warning}`}><AlertTriangle size={15} aria-hidden="true" /><span>{warning}</span></li>
              ))}
            </ul>
          </div>
        </section>
      ) : null}

      <section className="daily-health-banner">
        <div className="health-banner-status">
          <div className={`health-indicator ${healthLevel}`} />
          <div><h2 className="health-headline">{healthHeadline}</h2><p className="health-detail">{healthDetail}</p></div>
        </div>
        <div className="health-banner-meta">
          <span className="last-refreshed">Last refreshed: {payload?.snapshot.generated_at ? formatTimestamp(payload.snapshot.generated_at) : 'Never'}</span>
          <button className="icon-button" type="button" onClick={() => void onRefresh()} title="Refresh status" disabled={isRefreshing}><RefreshCw className={isRefreshing ? 'spin' : ''} size={18} /></button>
        </div>
      </section>

      <details className="source-panel-collapsible">
        <summary className="source-panel-summary"><span>Source: {sourceAccount}/{sourceContainer}</span>{scanRunning ? <span className="scan-badge">Scanning...</span> : null}</summary>
      <section className="source-panel">
        <div className="source-controls">
          <button
            className={sourceAccount === PROD_ACCOUNT && sourceContainer === PROD_CONTAINER ? 'active' : ''}
            type="button"
            onClick={() => {
              onSourceAccount(PROD_ACCOUNT);
              onSourceContainer(PROD_CONTAINER);
            }}
          >
            Production Blob
          </button>
          <button
            className={sourceAccount === TEST_ACCOUNT && sourceContainer === TEST_CONTAINER ? 'active' : ''}
            type="button"
            onClick={() => {
              onSourceAccount(TEST_ACCOUNT);
              onSourceContainer(TEST_CONTAINER);
            }}
          >
            Test Blob
          </button>
          <label>
            Account
            <input list="storage-accounts" value={sourceAccount} onChange={(event) => onSourceAccount(event.target.value)} placeholder="Search storage accounts" />
            <datalist id="storage-accounts">
              {accounts.map((account) => <option value={account} key={account} />)}
            </datalist>
          </label>
          <label>
            Container
            <input list="storage-containers" value={sourceContainer} onChange={(event) => onSourceContainer(event.target.value)} placeholder="Search containers" />
            <datalist id="storage-containers">
              {containers.map((container) => <option value={container} key={container} />)}
            </datalist>
          </label>
        </div>
        <div className="actions">
          <button className="icon-button" type="button" onClick={() => void onRefresh()} title="Refresh status" disabled={isRefreshing}>
            <RefreshCw className={isRefreshing ? 'spin' : ''} size={18} />
          </button>
          <button className="primary-button" type="button" onClick={() => void onStartScan()} disabled={scanRunning}>
            {scanRunning ? <Loader2 className="spin" size={17} /> : <Database size={17} />}
            <span>{scanRunning
              ? `Scanning ${scanProgress?.completed_machines ?? 0}/${scanProgress?.total_machines || '?'} machines · ${machineScanPercent}% (${formatElapsedTime(scanElapsed)})`
              : 'Run Scan'}</span>
          </button>
          {scanRunning ? (
            <button className="icon-button danger" type="button" onClick={() => void onResetScan()} title="Cancel and reset scan">
              <X size={17} />
            </button>
          ) : null}
        </div>
      </section>
      </details>
      {scanRunning ? (
        <p className="source-scan-status">
          {scanProgress?.phase ?? 'starting'}: {scanProgress?.source_account}/{scanProgress?.source_container}
        </p>
      ) : null}
      {sourceError ? <section className="notice source-notice"><AlertTriangle size={16} /><span>{sourceError}</span></section> : null}

      <section className="kpi-grid">
        {dashboardLoading || scanRunning ? <LoadingMetrics /> : <>
          <MetricCard label="Verified Coverage" value={coveragePercent(summary)} detail={`${formatNumber(summary?.completed_eligible_partitions ?? 0)} of ${formatNumber(summary?.eligible_partitions ?? 0)} eligible partitions`} tone={coveragePercentValue(summary) === 100 ? 'success' : 'neutral'} />
          <MetricCard label="Actionable Gaps" value={summary?.missing_partitions ?? 0} detail={`${summary?.needs_backfill ?? 0} machine${(summary?.needs_backfill ?? 0) === 1 ? '' : 's'} need review`} tone={(summary?.missing_partitions ?? 0) > 0 ? 'warning' : 'success'} />
          <MetricCard label="Inspection Errors" value={summary?.partitions_with_scan_errors ?? 0} detail={`${summary?.partitions_with_missing_features ?? 0} feature-quality gaps`} tone={(summary?.partitions_with_scan_errors ?? 0) > 0 ? 'warning' : 'success'} />
          <MetricCard label="Active Backfills" value={activeBackfillCount} detail={activeBackfillCount > 0 ? `${activeBackfillCount} machine${activeBackfillCount === 1 ? '' : 's'} currently processing` : 'No backfills running'} tone={activeBackfillCount > 0 ? 'neutral' : 'success'} />
        </>}
      </section>

      <section className="content-grid">
        <div className="panel machine-panel">
          <div className="panel-header">
            <div>
              <h2>Machine Coverage</h2>
              <p>{sourceLabel(payload?.snapshot)}</p>
            </div>
            <a className="export-link" href={`${API_BASE}/api/backfill/export/manifest?status=needs_backfill`}>
              <Download size={16} />
              <span>Manifest</span>
            </a>
          </div>

          <div className="filters">
            <label className="search-field">
              <Search size={16} />
              <input value={query} onChange={(event) => onQuery(event.target.value)} placeholder="Machine ID" />
            </label>
            <label className="select-field">
              <Filter size={16} />
              <select value={statusFilter} onChange={(event) => onStatusFilter(event.target.value as typeof statusFilter)}>
                {STATUS_OPTIONS.map((option) => (
                  <option value={option} key={option}>
                    {labelForStatus(option)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {dashboardLoading ? <LoadingWidget label="Loading machine coverage" /> : <>
          <div className={`table-wrap ${filterPulse ? 'filter-pulse' : ''}`}>
            <table>
              <thead>
                <tr>
                  <th>Machine</th>
                  <th>Status</th>
                  <th>Coverage</th>
                  <th>Installed</th>
                  <th>Online / Offline</th>
                  <th>First Gap</th>
                  <th>Rows</th>
                  <th>Active backfill</th>
                  <th>Backfill</th>
                  <th>Augury</th>
                  <th>Quick</th>
                </tr>
              </thead>
              <tbody>
                {paginatedMachines.map((machine) => (
                  <tr
                    key={machine.machine_id}
                    onClick={() => window.open(detailMachineHref(machine.machine_id), '_blank', 'noopener,noreferrer')}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        window.open(detailMachineHref(machine.machine_id), '_blank', 'noopener,noreferrer');
                      }
                    }}
                    role="button"
                    tabIndex={0}
                    title="Open detailed machine view in a new tab"
                  >
                    <td className="machine-id-cell"><span className="mono">{machine.machine_id}</span><TestMachineBadge isTest={machine.is_test_machine} /><CopyMachineIdButton machineId={machine.machine_id} onCopied={() => onToast('success', 'Machine ID copied.')} /></td>
                    <td><StatusPill status={machine.status} /></td>
                    <td><Progress value={machine.months_complete} max={machine.months_expected} /></td>
                    <td>{machine.installation_at ? formatTimestamp(machine.installation_at) : 'Unknown'}</td>
                    <td><span className="activity-count online">{machine.online_months ?? 0} online</span> <span className="activity-count offline">{machine.offline_months ?? 0} offline</span></td>
                    <td>{machine.first_missing_month ?? 'None'}</td>
                    <td>{formatNumber(machine.production_rows)}</td>
                    <td>{activeByMachine[machine.machine_id]?.filter((item) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(item.state)).length ? <span className="task-phase-badge running">{activeByMachine[machine.machine_id].filter((item) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(item.state)).map((item) => `${item.year}-${String(item.month).padStart(2, '0')}`).join(', ')}</span> : '—'}</td>
                    <td>
                      <QuickBackfillTrigger machine={machine} activeMonths={activeByMachine[machine.machine_id] ?? []} onToast={onToast} workflowMutationsEnabled={workflowMutationsEnabled} />
                    </td>
                    <td>
                      <a className="icon-link" href={machine.augury_url} target="_blank" rel="noreferrer" title="Open machine in Augury" onClick={(event) => event.stopPropagation()}>
                        <ExternalLink size={16} />
                      </a>
                    </td>
                    <td>
                      <button className="icon-link" type="button" title="Open quick machine view" onClick={(event) => {
                        event.stopPropagation();
                        setQuickMachine(machine);
                      }}>
                        <Eye size={16} />
                      </button>
                    </td>
                  </tr>
                ))}
                {scanRunning ? Array.from({ length: Math.max(3, 8 - paginatedMachines.length) }, (_, index) => <tr className="scan-table-skeleton" key={`loading-${index}`}><td colSpan={11}><i /></td></tr>) : null}
              </tbody>
            </table>
          </div>
          {totalPages > 1 && (
            <div className="pagination" aria-label="Machine coverage pagination">
              <button className="pagination-btn" type="button" disabled={currentPage === 1} onClick={() => setCurrentPage(1)} title="First page">«</button>
              <button className="pagination-btn" type="button" disabled={currentPage === 1} onClick={() => setCurrentPage((page) => Math.max(1, page - 1))} title="Previous page">‹</button>
              <span className="pagination-info">Page {currentPage} of {totalPages}<span className="pagination-total">({filteredMachines.length} machines)</span></span>
              <button className="pagination-btn" type="button" disabled={currentPage === totalPages} onClick={() => setCurrentPage((page) => Math.min(totalPages, page + 1))} title="Next page">›</button>
              <button className="pagination-btn" type="button" disabled={currentPage === totalPages} onClick={() => setCurrentPage(totalPages)} title="Last page">»</button>
            </div>
          )}
          </>}
        </div>
      </section>

      {quickMachine ? <QuickMachineModal machine={quickMachine} onClose={() => setQuickMachine(null)} /> : null}
    </>
  );
}

function ActiveBackfillSummary({ machineId }: { machineId: string }) {
  const [months, setMonths] = useState<ActiveMonth[]>([]);
  useEffect(() => { fetch(`${API_BASE}/api/machines/${encodeURIComponent(machineId)}/backfills/active`).then(async (response) => response.ok ? response.json() as Promise<{ months: ActiveMonth[] }> : { months: [] }).then((data) => setMonths(data.months)).catch(() => setMonths([])); }, [machineId]);
  const active = months.filter((item) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(item.state));
  return active.length ? <section className="notice active-backfill-notice"><Zap size={16} /><span>Backfilling: {active.map((item) => `${item.year}-${String(item.month).padStart(2, '0')} (${item.state.replace('_', ' ')})`).join(', ')}</span></section> : null;
}

function QuickMachineModal({ machine, onClose }: { machine: MachineStatus; onClose: () => void }) {
  return <div className="machine-quick-modal-backdrop" role="presentation" onMouseDown={onClose}><section className="machine-quick-modal" role="dialog" aria-modal="true" aria-labelledby="quick-machine-title" onMouseDown={(event) => event.stopPropagation()}><div className="panel-header"><div><p className="eyebrow">Quick machine view</p><div className="machine-title-row"><h2 id="quick-machine-title">{machine.machine_id}</h2><TestMachineBadge isTest={machine.is_test_machine} /><CopyMachineIdButton machineId={machine.machine_id} /></div><p>Installed {machine.installation_at ? formatTimestamp(machine.installation_at) : 'date unknown'} · first FST data {machine.coverage_start_month ?? 'not observed'}</p></div><div className="actions"><a className="export-link" href={detailMachineHref(machine.machine_id)} target="_blank" rel="noreferrer"><ExternalLink size={16} /><span>Full details</span></a><button className="icon-button" type="button" onClick={onClose} title="Close quick view"><X size={18} /></button></div></div><ActiveBackfillSummary machineId={machine.machine_id} /><MachineDetails machine={machine} compact /></section></div>;
}

function MachineQuickViewPage({ machine, machineId, darkMode, onToggleTheme }: { machine: MachineStatus | null; machineId: string; darkMode: boolean; onToggleTheme: () => void }) {
  return <main className={`dashboard-shell quick-view-window ${darkMode ? 'dark' : ''}`}><header className="topbar"><div><p className="eyebrow">Quick machine view</p><div className="machine-title-row"><h1>{machineId}</h1><TestMachineBadge isTest={machine?.is_test_machine} /><CopyMachineIdButton machineId={machineId} /></div></div><div className="actions"><button className="icon-button theme-toggle" type="button" onClick={onToggleTheme} title={darkMode ? 'Use light theme' : 'Use dark theme'}>{darkMode ? <Sun size={18} /> : <Moon size={18} />}</button><button className="secondary-button" type="button" onClick={() => window.close()}><X size={16} /><span>Close</span></button>{machine ? <a className="export-link" href={detailMachineHref(machine.machine_id)} target="_blank" rel="noreferrer"><ExternalLink size={16} /><span>Full details</span></a> : null}</div></header>{machine ? <section className="panel quick-machine-panel"><div className="panel-header"><div><h2>Installed {machine.installation_at ? formatTimestamp(machine.installation_at) : 'date unknown'}</h2><p>First FST data {machine.coverage_start_month ?? 'not observed'} · {machine.reason}</p></div><a className="export-link" href={machine.augury_url} target="_blank" rel="noreferrer"><ExternalLink size={16} /><span>Augury</span></a></div><ActiveBackfillSummary machineId={machine.machine_id} /><MachineDetails machine={machine} compact /></section> : <section className="panel"><EmptyState message={`Machine ${machineId} was not found in the current scan payload.`} /></section>}</main>;
}

function QuickBackfillTrigger({ machine, activeMonths, onToast, workflowMutationsEnabled }: { machine: MachineStatus; activeMonths: ActiveMonth[]; onToast: (type: Toast['type'], msg: string) => void; workflowMutationsEnabled: boolean }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [submissionAction, setSubmissionAction] = useState<AdminAction | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const defaultRange = defaultBackfillRange(machine);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  useEffect(() => {
    if (!submissionAction?.id || !isTriggerPreparationActive(submissionAction)) return;
    const actionId = submissionAction.id;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/admin/actions/${encodeURIComponent(actionId)}`, { cache: 'no-store' });
        const payload = await response.json() as { action?: AdminAction };
        if (!response.ok || !payload.action) throw new Error('Could not refresh backfill submission status.');
        if (cancelled) return;
        setSubmissionAction(payload.action);
        if (payload.action.phase === 'accepted') onToast('success', `Backfill accepted for ${machine.machine_id.slice(-8)}.`);
        if (payload.action.phase === 'failed' || payload.action.status === 'failed') onToast('error', payload.action.error ?? 'Backfill submission failed.');
        if (!isTriggerPreparationActive(payload.action)) return;
      } catch (error) {
        void error;
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 1000);
    };
    void poll();
    return () => { cancelled = true; if (timer !== undefined) window.clearTimeout(timer); };
  }, [submissionAction?.id, submissionAction?.phase, submissionAction?.status, machine.machine_id]);

  const trigger = async (env: EnvironmentTarget) => {
    if (activeMonths.some((item) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(item.state))) {
      onToast('error', `Backfill already active for ${activeMonths.filter((item) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(item.state)).map((item) => `${item.year}-${String(item.month).padStart(2, '0')}`).join(', ')}. Open the machine Backfill tab to manage it.`);
      setOpen(false);
      return;
    }
    const range = defaultBackfillRange(machine);
    if (!range) {
      onToast('error', 'No online eligible months are available for backfill.');
      setOpen(false);
      return;
    }
    setBusy(true);
    try {
      const triggerRes = await fetch(`${API_BASE}/api/admin/backfills/orchestrated`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          machine_ids: [machine.machine_id],
          since: range.since,
          until: range.until,
          manifest_prefix: '',
          params: {
            environment: env,
            namespace: env === 'prod' ? PROD_CONTAINER : DEFAULT_DEV_NAMESPACE,
            storage_account_manifest_path: '',
            include_features_to_backfill: false,
            features_to_backfill: [],
            max_parallel_steps: 1,
            force_sessions_from_bucket: false,
            confirm_production: env === 'prod',
            confirmation_text: env === 'prod' ? PROD_CONFIRMATION : '',
          },
        }),
      });
      const triggerResult = await triggerRes.json() as { action?: AdminAction; detail?: string };
      if (!triggerRes.ok) throw new Error(triggerResult.detail ?? `Trigger failed: ${triggerRes.status}`);
      if (!triggerResult.action?.id) throw new Error('The backfill request was accepted without an action ID.');
      setSubmissionAction(triggerResult.action);
      onToast('info', `Backfill request accepted for ${machine.machine_id.slice(-8)}; preparation is starting.`);
      setOpen(false);
    } catch (err) {
      onToast('error', err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="quick-backfill" ref={ref} onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
      <button
        className={`quick-backfill-toggle ${open ? 'open' : ''}`}
        type="button"
        title={!workflowMutationsEnabled ? 'Workflow operations are disabled in monitor-only runtime' : defaultRange ? 'Trigger backfill for the first online eligible month' : 'No online eligible month; backfill is unavailable'}
        aria-label={!workflowMutationsEnabled ? 'Backfill unavailable: monitor-only runtime' : defaultRange ? 'Trigger backfill for the first online eligible month' : 'Backfill unavailable: no online eligible month'}
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}
        disabled={!workflowMutationsEnabled || busy || !defaultRange || isTriggerPreparationActive(submissionAction)}
      >
        {busy ? <Loader2 className="spin" size={14} /> : <Zap size={14} />}
        <ChevronDown size={12} className={`chevron ${open ? 'flipped' : ''}`} />
      </button>
      {open && (
        <div className="quick-backfill-dropdown animate-dropdown">
          <button className="quick-backfill-option dev" type="button" onClick={() => void trigger('dev')} disabled={busy}>
            <Play size={13} />
            <span>Dev</span>
          </button>
          <button className="quick-backfill-option prod" type="button" onClick={() => void trigger('prod')} disabled={busy}>
            <Play size={13} />
            <span>Production</span>
          </button>
        </div>
      )}
      {submissionAction ? <span className={`quick-trigger-progress${submissionAction.phase === 'failed' ? ' failed' : ''}`} role="status" aria-live="polite" title={submissionAction.error ?? undefined}>{submissionAction.phase === 'failed' ? `${triggerPhaseLabel(submissionAction.phase)}: ${submissionAction.error ?? 'submission failed'}` : triggerPhaseLabel(submissionAction.phase)}</span> : null}
    </div>
  );
}

function MachineDetailPage({
  payload,
  machine,
  machineId,
  apiError,
  onRefresh,
  isRefreshing,
  darkMode,
  onToggleTheme,
  onToast,
  workflowMutationsEnabled,
}: {
  payload: StatusPayload | null;
  machine: MachineStatus | null;
  machineId: string;
  apiError: string | null;
  onRefresh: () => Promise<void>;
  isRefreshing: boolean;
  darkMode: boolean;
  onToggleTheme: () => void;
  onToast: (type: Toast['type'], message: string) => void;
  workflowMutationsEnabled: boolean;
}) {
  const [machineView, setMachineView] = useState<MachineView>('overview');
  const [activeMonths, setActiveMonths] = useState<Record<number, ActiveMonth>>({});
  const loadActive = useCallback(async () => {
    if (!machine) return;
    try {
      const response = await fetch(`${API_BASE}/api/machines/${encodeURIComponent(machine.machine_id)}/backfills/active`);
      if (!response.ok) throw new Error('Active backfill state is unavailable');
      const data = await response.json() as { months: ActiveMonth[] };
      setActiveMonths(Object.fromEntries(data.months.map((item) => [item.month_index, item])));
    } catch (error) { onToast('error', error instanceof Error ? error.message : String(error)); }
  }, [machine, onToast]);
  useEffect(() => { void loadActive(); }, [loadActive]);
  const activeLabels = Object.values(activeMonths).filter((item) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(item.state)).map((item) => `${item.year}-${String(item.month).padStart(2, '0')} (${item.state.replace('_', ' ')})`);
  return (
    <main className={`dashboard-shell detail-shell ${darkMode ? 'dark' : ''}`}>
      <header className="topbar">
        <div>
          <p className="eyebrow">Machine Details</p>
          <div className="machine-title-row"><h1 className="machine-title">{machineId}</h1><TestMachineBadge isTest={machine?.is_test_machine} /><CopyMachineIdButton machineId={machineId} /></div>
          <p className="subtitle">{sourceLabel(payload?.snapshot)}</p>
        </div>
        <div className="actions">
          <a className="export-link" href={monitorHref()}>
            <ArrowLeft size={16} />
            <span>Monitor</span>
          </a>
          <button className="icon-button" type="button" onClick={() => void onRefresh()} title="Refresh status" disabled={isRefreshing}>
            <RefreshCw className={isRefreshing ? 'spin' : ''} size={18} />
          </button>
          <button className="icon-button theme-toggle" type="button" onClick={onToggleTheme} title={darkMode ? 'Use light theme' : 'Use dark theme'}>
            {darkMode ? <Sun size={18} /> : <Moon size={18} />}
          </button>
          {machine ? (
            <a className="export-link" href={machine.augury_url} target="_blank" rel="noreferrer">
              <ExternalLink size={16} />
              <span>Augury</span>
            </a>
          ) : null}
        </div>
      </header>

      {apiError ? (
        <section className="notice error">
          <ShieldAlert size={18} />
          <span>{apiError}. Start the backend at {API_BASE}.</span>
        </section>
      ) : null}

      {!workflowMutationsEnabled ? <section className="notice inline-notice runtime-mode-notice" role="status"><ShieldAlert size={17} /><span>Monitor-only mode: workflow submissions, stops, and CLI log lookups are disabled. Standalone manifest creation remains available.</span></section> : null}

      {machine ? (
        <div className="machine-detail-workspace">
          <aside className="machine-detail-sidebar" aria-label="Machine detail sections">
            <p>Machine workspace</p>
            <button className={machineView === 'overview' ? 'active' : ''} type="button" onClick={() => setMachineView('overview')}><Eye size={16} />Overview</button>
            <button className={machineView === 'features' ? 'active' : ''} type="button" onClick={() => setMachineView('features')}><Activity size={16} />Feature graph</button>
            <button className={machineView === 'backfill' ? 'active' : ''} type="button" onClick={() => setMachineView('backfill')}><Play size={16} />Backfill</button>
            <button className={machineView === 'running' ? 'active' : ''} type="button" onClick={() => setMachineView('running')}><Zap size={16} />Running tasks</button>
            <button className={machineView === 'history' ? 'active' : ''} type="button" onClick={() => setMachineView('history')}><History size={16} />Run history</button>
            <button className={machineView === 'observability' ? 'active' : ''} type="button" onClick={() => setMachineView('observability')}><Search size={16} />Observability</button>
          </aside>
          <div className="machine-detail-content">
          {machineView === 'overview' ? <>
          {activeLabels.length ? <section className="notice active-backfill-notice"><Zap size={18} /><span>Backfilling now: {activeLabels.join(', ')}</span></section> : null}
          <section className="kpi-grid">
            <MetricCard label="Status" value={labelForStatus(machine.status)} detail={machine.reason} tone={machine.status === 'backfilled' ? 'success' : 'warning'} />
            <MetricCard label="Installed" value={machine.installation_month ?? 'Unknown'} detail={machine.installation_at ? formatTimestamp(machine.installation_at) : 'Mongo installation date unavailable'} tone="neutral" />
            <MetricCard label="Verified Coverage" value={`${machine.months_complete}/${machine.months_expected}`} detail={`${machine.online_months ?? 0} online · ${machine.offline_months ?? 0} offline · ${machine.pre_install_months ?? 0} pre-install`} tone={machine.months_complete === machine.months_expected ? 'success' : 'warning'} />
            <MetricCard label="Actionable Gap" value={machine.first_missing_month ?? 'None'} detail={machine.first_missing_month ? machine.recommended_action : 'No missing eligible partition'} tone={machine.first_missing_month ? 'warning' : 'success'} />
            <MetricCard label="FST Rows" value={formatNumber(machine.production_rows)} detail={machine.last_populated_month ? `last populated ${machine.last_populated_month}` : 'no populated month'} tone="neutral" />
          </section>
          <section className="panel"><div className="panel-header"><div><p className="eyebrow">Coverage overview</p><h2>Monthly partition health</h2><p>Review coverage and select a month to inspect its status.</p></div></div><MachineDetails machine={machine} activeMonths={activeMonths} compact /></section>
          </> : null}
          {machineView === 'features' ? <MachineFeatureChart machine={machine} /> : null}
          {machineView === 'backfill' ? <MachineBackfillPanel machine={machine} onToast={onToast} workflowMutationsEnabled={workflowMutationsEnabled} /> : null}
          {machineView === 'running' ? <MachineRuns machineId={machine.machine_id} mode="running" /> : null}
          {machineView === 'history' ? <MachineRuns machineId={machine.machine_id} mode="history" /> : null}
          {machineView === 'observability' ? <MachineTaskLogs machineId={machine.machine_id} /> : null}
          </div>
        </div>
      ) : (
        <section className="panel">
          {payload ? <EmptyState message={`Machine ${machineId} was not found in the current scan payload.`} /> : <LoadingWidget label="Loading machine details" />}
        </section>
      )}
    </main>
  );
}

function MachineBackfillPanel({ machine, onToast, workflowMutationsEnabled }: { machine: MachineStatus; onToast: (type: Toast['type'], message: string) => void; workflowMutationsEnabled: boolean }) {
  // Disabled until the parent pod receives an authorized control-store credential.
  const perMonthStopsEnabled = false;
  const [selectedMonthIndices, setSelectedMonthIndices] = useState<number[]>([]);
  const [activeMonths, setActiveMonths] = useState<Record<number, ActiveMonth>>({});
  const [submissionAction, setSubmissionAction] = useState<AdminAction | null>(null);
  const selectedMonths = machine.months
    .filter((month) => isMonthBackfillable(month) && selectedMonthIndices.includes(orchestratorMonthIndex(month.partition)))
    .sort((left, right) => orchestratorMonthIndex(left.partition) - orchestratorMonthIndex(right.partition));
  const selectedMonthLabels = selectedMonths.map((month) => month.partition.label);
  const selectedRanges = backfillRangesForMonths(selectedMonths);
  const selectMonths = (predicate: (month: MonthStatus) => boolean) => {
    setSelectedMonthIndices(machine.months.filter(predicate).map((month) => orchestratorMonthIndex(month.partition)));
  };
  const [spec, setSpec] = useState<AdminSpec | null>(null);
  const mutationsEnabled = workflowMutationsEnabled;
  const [manifestPrefix, setManifestPrefix] = useState('');
  const [trigger, setTrigger] = useState<TriggerForm>({
    environment: 'dev',
    namespace: DEFAULT_DEV_NAMESPACE,
    storage_account_manifest_path: '',
    include_features_to_backfill: false,
    features_to_backfill: '',
    max_parallel_steps: 1,
    force_sessions_from_bucket: false,
    confirm_production: false,
    confirmation_text: '',
  });
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [createdManifest, setCreatedManifest] = useState<ManifestResult | null>(null);
  const hasActiveBackfill = Object.values(activeMonths).some((item) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(item.state));
  // Queued work is shown as active, but only the child actually executing is
  // highlighted as the running month in the calendar.
  const runningMonthIndex = Object.values(activeMonths).find((item) => item.state === 'running')?.month_index;

  useEffect(() => {
    const onlineIndices = new Set(machine.months.filter(isMonthBackfillable).map((month) => orchestratorMonthIndex(month.partition)));
    setSelectedMonthIndices((current) => {
      const filtered = current.filter((index) => onlineIndices.has(index));
      return filtered.length === current.length ? current : filtered;
    });
  }, [machine.machine_id, machine.months]);

  useEffect(() => {
    let cancelled = false;
    getCachedJson<AdminSpec>('admin-spec', `${API_BASE}/api/admin/spec`, 30 * 60_000)
      .then((loadedSpec) => {
        if (cancelled) {
          return;
        }
        setSpec(loadedSpec);
        setTrigger((current) => ({
          ...current,
          namespace: current.namespace || loadedSpec.default_dev_namespace,
        }));
        setError(null);
      })
      .catch((loadError) => {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : String(loadError));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshActiveMonths = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/machines/${encodeURIComponent(machine.machine_id)}/backfills/active`);
      if (!response.ok) throw new Error(`Could not load active months: ${response.status}`);
      const data = await response.json() as { months: ActiveMonth[] };
      setActiveMonths(Object.fromEntries(data.months.map((item) => [item.month_index, item])));
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    }
  }, [machine.machine_id]);
  useEffect(() => { void refreshActiveMonths(); }, [refreshActiveMonths]);
  useEffect(() => {
    if (!isTriggerPreparationActive(submissionAction) || !submissionAction?.id) return;
    const actionId = submissionAction.id;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/admin/actions/${encodeURIComponent(actionId)}`, { cache: 'no-store' });
        const payload = await response.json() as { action?: AdminAction; detail?: string };
        if (!response.ok || !payload.action) throw new Error(payload.detail ?? 'Could not refresh backfill submission status.');
        if (cancelled) return;
        setSubmissionAction(payload.action);
        if (payload.action.phase === 'accepted') {
          onToast('success', `Backfill accepted for ${selectedMonthLabels.join(', ')}.`);
          await refreshActiveMonths();
          return;
        }
        if (payload.action.phase === 'failed' || payload.action.status === 'failed') {
          const message = payload.action.error ?? 'Backfill submission failed.';
          setError(message);
          onToast('error', message);
          await refreshActiveMonths();
          return;
        }
      } catch {
        // Keep polling transient status-read failures without obscuring the current phase.
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 1000);
    };
    void poll();
    return () => { cancelled = true; if (timer !== undefined) window.clearTimeout(timer); };
  }, [submissionAction?.id, submissionAction?.phase, submissionAction?.status, machine.machine_id, refreshActiveMonths]);
  const submissionActive = isTriggerPreparationActive(submissionAction);

  const createManifest = async () => {
    if (!selectedMonths.length) {
      setError('Select at least one eligible month before creating manifests.');
      return;
    }
    setBusyAction('manifest');
    try {
      const response = await fetch(`${API_BASE}/api/admin/manifests/orchestrated`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          machine_ids: [machine.machine_id],
          manifest_prefix: manifestPrefix,
          month_indices_by_machine: {
            [machine.machine_id]: selectedMonths.map((month) => orchestratorMonthIndex(month.partition)),
          },
        }),
      });
      const result = await response.json() as { manifest?: ManifestResult; detail?: string };
      if (!response.ok || !result.manifest) throw new Error(result.detail ?? `Request failed: ${response.status}`);
      setCreatedManifest(result.manifest);
      setManifestPrefix(result.manifest.manifest_prefix ?? manifestPrefix);
      setError(null);
      onToast('success', `Created ${result.manifest.rows} monthly manifest${result.manifest.rows === 1 ? '' : 's'} without starting a backfill.`);
    } catch (manifestError) {
      const message = manifestError instanceof Error ? manifestError.message : String(manifestError);
      setError(message);
      onToast('error', `Manifest creation failed: ${message}`);
    } finally {
      setBusyAction(null);
    }
  };

  const createAndTrigger = async () => {
    if (!mutationsEnabled) return;
    if (submissionActive) return;
    if (Object.values(activeMonths).some((item) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(item.state))) {
      onToast('error', 'This machine already has an active backfill. Wait for it to finish or stop it before starting another.');
      return;
    }
    if (!selectedRanges.length) {
      setError('Select at least one eligible month before starting a backfill.');
      return;
    }
    setBusyAction('trigger');
    try {
      const response = await fetch(`${API_BASE}/api/admin/backfills/orchestrated`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          machine_ids: [machine.machine_id],
          manifest_prefix: manifestPrefix,
          // Use the calendar month, not the scan-relative display index. This
          // also makes an already-open pre-fix scan safe to submit.
          month_indices_by_machine: { [machine.machine_id]: selectedMonths.map((month) => orchestratorMonthIndex(month.partition)) },
          params: buildTriggerParams(trigger, spec?.prod_confirmation ?? PROD_CONFIRMATION),
        }),
      });
      const result = await response.json() as { action?: AdminAction; detail?: string };
      if (!response.ok) {
        throw new Error(result.detail ?? `Request failed: ${response.status}`);
      }
      if (!result.action?.id) throw new Error('The backfill request was accepted without an action ID.');
      setSubmissionAction(result.action);
      setError(null);
      onToast('info', `Backfill request accepted; preparing ${selectedMonthLabels.join(', ')}.`);
    } catch (triggerError) {
      const message = triggerError instanceof Error ? triggerError.message : String(triggerError); setError(message); onToast('error', message);
    } finally {
      setBusyAction(null);
    }
  };

  const stopAndRequeue = async (active: ActiveMonth) => {
    if (!mutationsEnabled) return;
    const downstream: number[] = [];
    const child = active.child_workflow_id || 'the current child workflow';
    const parent = active.parent_workflow_id || 'the parent workflow';
    if (!window.confirm(`Stop ${machine.months.find((month) => orchestratorMonthIndex(month.partition) === active.month_index)?.partition.label ?? active.month_index} (${child}) in parent ${parent}? The parent will skip this month and continue to the next scheduled month, or finish cleanly if there is none.`)) return;
    onToast('info', `Requesting stop for month ${active.year}-${String(active.month).padStart(2, '0')}…`);
    setBusyAction(`cancel-${active.month_index}`);
    try {
      const response = await fetch(`${API_BASE}/api/machines/${encodeURIComponent(machine.machine_id)}/backfills/${active.month_index}/cancel`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ parent_workflow_id: active.parent_workflow_id ?? '', child_workflow_id: active.child_workflow_id ?? '', requeue_month_indices: downstream, requested_by: 'dashboard' }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? `Cancel failed: ${response.status}`);
      setError('Cancellation accepted. The parent will continue with the next scheduled month or finish cleanly.');
      onToast('success', 'Month stop accepted. The parent will continue with the next scheduled month or finish cleanly.');
      await refreshActiveMonths();
    } catch (cancelError) {
      const message = cancelError instanceof Error ? cancelError.message : String(cancelError); setError(message); onToast('error', message);
    } finally {
      setBusyAction(null);
    }
  };

  const stopEntireBackfill = async () => {
    if (!mutationsEnabled) return;
    const parents = [...new Set(Object.values(activeMonths).map((item) => item.parent_workflow_id).filter(Boolean))];
    const parent = parents[0];
    if (!parent) { onToast('error', 'No parent workflow ID is available yet. Refresh and try again.'); return; }
    const confirmation = window.prompt(`This terminates the entire backfill parent (${parent}) and stops all remaining months. Type the workflow ID to confirm.`);
    if (confirmation !== parent) { if (confirmation !== null) onToast('error', 'Workflow ID did not match; termination was not sent.'); return; }
    setBusyAction('terminate-parent'); onToast('info', 'Submitting full backfill termination…');
    try {
      const response = await fetch(`${API_BASE}/api/admin/workflows/terminate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ params: { environment: 'dev', namespace: DEFAULT_DEV_NAMESPACE, workflow_id: parent } }) });
      const result = await response.json(); if (!response.ok) throw new Error(result.detail ?? `Termination failed: ${response.status}`);
      onToast('success', 'Full backfill termination submitted.'); await refreshActiveMonths();
    } catch (error) { onToast('error', error instanceof Error ? error.message : String(error)); }
    finally { setBusyAction(null); }
  };

  return (
    <section className="panel details-panel">
      <div className="panel-header">
        <div>
          <h2>Backfill This Machine</h2>
          <p className="mono">{machine.machine_id}</p>
          <p>Creates one manifest per month and runs the ULRPM parent orchestrator sequentially against the selected target.</p>
        </div>
        <div className="actions">
        {hasActiveBackfill ? <button className="secondary-button danger" type="button" disabled={!mutationsEnabled || busyAction === 'terminate-parent'} title={!mutationsEnabled ? 'Workflow operations are disabled in monitor-only runtime' : undefined} onClick={() => void stopEntireBackfill()}>{busyAction === 'terminate-parent' ? <Loader2 className="spin" size={16} /> : <Square size={16} />}Stop entire backfill</button> : null}
        {spec ? (
          <a className="export-link" href={spec.outerbounds_running_url} target="_blank" rel="noreferrer">
            <ExternalLink size={16} />
            <span>Running</span>
          </a>
        ) : null}
        </div>
      </div>
      {error ? (
        <section className="notice error inline-notice">
          <ShieldAlert size={18} />
          <span>{error}</span>
        </section>
      ) : null}
      {submissionAction ? <section className={`notice inline-notice trigger-progress-inline${submissionAction.phase === 'failed' ? ' failed' : ''}`} role="status" aria-live="polite">
        {submissionActive ? <Loader2 className="spin" size={17} /> : submissionAction.phase === 'failed' ? <ShieldAlert size={17} /> : <CheckCircle2 size={17} />}
        <span>Backfill {triggerPhaseLabel(submissionAction.phase)} · action {submissionAction.id}{submissionAction.error ? ` · ${submissionAction.error}` : ''}</span>
      </section> : null}
      <section className="panel details-panel month-selector-panel">
        <div className="panel-header">
          <div>
            <h2>Select Months to Backfill</h2>
            <p>Choose one month, a range, or non-contiguous months. Only those month partitions are queued.</p>
          </div>
          <div className="month-selection-actions">
            <button className="secondary-button" type="button" disabled={hasActiveBackfill || submissionActive} onClick={() => selectMonths((month) => month.status === 'needs_backfill' && isMonthBackfillable(month))}>Select gaps</button>
            <button className="secondary-button" type="button" disabled={hasActiveBackfill || submissionActive} onClick={() => selectMonths(isMonthBackfillable)}>Select all eligible</button>
            <button className="secondary-button" type="button" onClick={() => setSelectedMonthIndices([])} disabled={selectedMonths.length === 0 || hasActiveBackfill || submissionActive}><X size={15} />Clear</button>
            <button className="secondary-button" type="button" onClick={() => void createManifest()} disabled={selectedMonths.length === 0 || busyAction !== null || submissionActive}>
              {busyAction === 'manifest' ? <Loader2 className="spin" size={16} /> : <UploadCloud size={16} />}
              <span>Create Manifest</span>
            </button>
            <button className="primary-button" type="button" onClick={() => void createAndTrigger()} disabled={!mutationsEnabled || selectedMonths.length === 0 || busyAction !== null || hasActiveBackfill || submissionActive || (trigger.environment === 'prod' && !trigger.confirm_production)} title={!mutationsEnabled ? 'Workflow operations are disabled in monitor-only runtime' : hasActiveBackfill ? 'A backfill is already active for this machine' : submissionActive ? 'Backfill preparation is already in progress' : trigger.environment === 'prod' && !trigger.confirm_production ? 'Check the production confirmation checkbox to enable this run' : undefined}>
              {busyAction === 'trigger' ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
              <span>Backfill {selectedMonths.length || ''} Selected</span>
            </button>
          </div>
        </div>
        <MachineDetails
          machine={machine}
          activeMonths={activeMonths}
          backfillDisabled={hasActiveBackfill || submissionActive}
          runningMonthIndex={runningMonthIndex}
          selectedMonthIndices={selectedMonthIndices}
          onToggleBackfillMonth={(month) => { if (!isMonthBackfillable(month)) return; const index = orchestratorMonthIndex(month.partition); setSelectedMonthIndices((current) => current.includes(index) ? current.filter((item) => item !== index) : [...current, index]); }}
        />
        <section className="month-selection-summary" aria-live="polite">
          <div>
            <strong>{selectedMonths.length ? `${selectedMonths.length} month${selectedMonths.length === 1 ? '' : 's'} ready to backfill` : 'Select month cards to begin'}</strong>
            <span>{selectedMonths.length ? `${selectedMonthLabels.join(', ')} · queues ${selectedRanges.length} sequential workflow${selectedRanges.length === 1 ? '' : 's'} and skips unselected months.` : 'Offline, not-installed, and unknown months are disabled to protect the backfill pipeline.'}</span>
          </div>
          {selectedMonths.length ? <Check size={20} aria-label="Selection ready" /> : null}
        </section>
      </section>
      {Object.values(activeMonths).some((item) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(item.state)) ? <section className="notice inline-notice"><Zap size={18} /><span>Backfilling: active months are locked to prevent duplicate submissions. Open the Running tab for workflow details.</span></section> : null}
        {Object.values(activeMonths).filter((item) => ['queued', 'running'].includes(item.state)).map((active) => <div className="actions" key={active.month_index}><span className={`task-phase-badge ${active.state}`}>{machine.months.find((month) => orchestratorMonthIndex(month.partition) === active.month_index)?.partition.label ?? active.month_index} · {active.state}</span>{perMonthStopsEnabled ? <button className="secondary-button danger" type="button" disabled={!mutationsEnabled || busyAction === `cancel-${active.month_index}`} onClick={() => void stopAndRequeue(active)}>{busyAction === `cancel-${active.month_index}` ? <Loader2 className="spin" size={16} /> : <Square size={16} />}Stop month</button> : null}{active.parent_workflow_id ? <a className="icon-link" href={`${OUTERBOUNDS_BASE}?flow_id=UlrpmDevBackfillOrchestratorFlow`} target="_blank" rel="noreferrer" title={`Open ${active.parent_workflow_id} in Outerbounds`}><ExternalLink size={16} /></a> : null}</div>)}
      <div className="form-grid">
        <label>
          Environment
          <select value={trigger.environment} onChange={(event) => setTrigger({ ...trigger, environment: event.target.value as EnvironmentTarget })}>
            <option value="dev">Dev/Test</option>
            <option value="prod">Production</option>
          </select>
        </label>
        <label>
          Namespace
          <input
            value={trigger.environment === 'prod' ? spec?.prod_namespace ?? PROD_CONTAINER : trigger.namespace}
            disabled={trigger.environment === 'prod'}
            onChange={(event) => setTrigger({ ...trigger, namespace: event.target.value })}
          />
        </label>
        {trigger.environment === 'prod' ? (
          <>
            <label className="checkbox-field">
              <input type="checkbox" checked={trigger.confirm_production} onChange={(event) => setTrigger({ ...trigger, confirm_production: event.target.checked })} />
              Confirm production backfill
            </label>
          </>
        ) : null}
        <label className="wide">
          Monthly manifest directory
          <input value={manifestPrefix} onChange={(event) => setManifestPrefix(event.target.value)} placeholder="Auto-generated when empty" />
          <small>Optional. Selected months create their own manifests beneath this directory.</small>
        </label>
        <label>
          Manifest buckets
          <input type="number" min={1} value={trigger.max_parallel_steps} onChange={(event) => setTrigger({ ...trigger, max_parallel_steps: Number(event.target.value) })} />
        </label>
        <small className="wide">The same orchestrator template is used for both targets; only its container namespace changes. The child flow's existing is_prod check skips dev seeding in production.</small>
      </div>
      {createdManifest ? <ManifestCreatedDialog manifest={createdManifest} onClose={() => setCreatedManifest(null)} onCopy={() => { void navigator.clipboard.writeText(createdManifest.manifest_path); onToast('success', 'Manifest template copied.'); }} /> : null}
    </section>
  );
}

function AdminTab({ onToast, workflowMutationsEnabled }: { onToast: (type: Toast['type'], message: string) => void; workflowMutationsEnabled: boolean }) {
  const [spec, setSpec] = useState<AdminSpec | null>(null);
  const [actions, setActions] = useState<AdminAction[]>([]);
  const [runningWorkflows, setRunningWorkflows] = useState<RunningWorkflowsPayload | null>(null);
  const [workflowSummary, setWorkflowSummary] = useState<WorkflowSummaryPayload | null>(null);
  const [adminReadiness, setAdminReadiness] = useState<AdminReadiness | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [triggerSubmitting, setTriggerSubmitting] = useState(false);
  const [triggerSubmission, setTriggerSubmission] = useState<AdminAction | null>(null);
  const refreshedTriggerActionId = useRef<string | null>(null);
  const [manifest, setManifest] = useState<MachineManifestForm>({
    machine_id: '',
    since: '',
    until: '',
    manifest_path: '',
  });
  const [multiManifest, setMultiManifest] = useState<MultiMachineManifestForm>({
    machine_ids: '',
    since: '',
    until: '',
    manifest_path: '',
  });
  const [allMachines, setAllMachines] = useState<AllMachinesBackfillForm>({
    since: '',
    until: '',
    manifest_path: '',
  });
  const [createdManifest, setCreatedManifest] = useState<ManifestResult | null>(null);
  const [source, setSource] = useState<WorkflowSourceForm>({
    source_type: 'local',
    local_flow_path: '',
    github_repo_url: '',
    github_ref: 'master',
    github_flow_path: 'FSTBackfill_prod_flow.py',
  });
  const [trigger, setTrigger] = useState<TriggerForm>({
    environment: 'dev',
    namespace: '',
    storage_account_manifest_path: '',
    include_features_to_backfill: false,
    features_to_backfill: '',
    max_parallel_steps: 1,
    force_sessions_from_bucket: false,
    confirm_production: false,
    confirmation_text: '',
  });
  const [logs, setLogs] = useState<LogsForm>({ run_task_path: '', stream: 'stdout' });
  const [stop, setStop] = useState<StopForm>({
    environment: 'dev',
    namespace: '',
    workflow_id: '',
    confirm_production: false,
    confirmation_text: '',
  });
  const [adminError, setAdminError] = useState<string | null>(null);
  const [adminLoading, setAdminLoading] = useState(true);
  const [referenceRefreshing, setReferenceRefreshing] = useState(false);
  const [workflowRefreshing, setWorkflowRefreshing] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [backfillScope, setBackfillScope] = useState<BackfillScope>('one');
  const [clearedRunnerPlanKey, setClearedRunnerPlanKey] = useState('');
  const [runnerMonthMode, setRunnerMonthMode] = useState<'range' | 'gaps' | 'all' | 'manual'>('gaps');
  const [manualMonthIndices, setManualMonthIndices] = useState<number[]>([]);
  const [parallelMachineRuns, setParallelMachineRuns] = useState(false);
  const [adminView, setAdminView] = useState<AdminView>('overview');
  const [adminSidebarOpen, setAdminSidebarOpen] = useState(true);
  const [operatorNote, setOperatorNote] = useState(() => localStorage.getItem('ulrpm-admin-operator-note') ?? '');
  const [fullrlblForm, setFullrlblForm] = useState<FullRlblTestForm>({
    machine_ids: '',
    fst_namespace: DEFAULT_DEV_NAMESPACE,
    lst_namespace: 'severity-relabel-dev-ulrpm',
    pipeline_name: '',
    manifest_path: '',
    runtime_patch: false,
    persist_dev_lst: false,
    seed_dev_lst: false,
    feature_fetch_mode: 'legacy',
    test_mode: 'fetch_only',
    wide_range_since: '2024/08/30/00',
    wide_range_until: '2026/08/30/00',
    memory_mb: 8192,
  });
  const [fullrlblMachines, setFullrlblMachines] = useState<string[]>([]);
  const [runnerMachines, setRunnerMachines] = useState<MachineStatus[]>([]);
  const [runnerActiveMonths, setRunnerActiveMonths] = useState<Record<string, ActiveMonth[]>>({});
  const [runnerActiveMonthsLoading, setRunnerActiveMonthsLoading] = useState(false);
  const [runnerBusyMachines, setRunnerBusyMachines] = useState<Record<string, BusyMachineInfo>>({});
  const [runnerBusyLoading, setRunnerBusyLoading] = useState(false);
  const [runnerMachineQuery, setRunnerMachineQuery] = useState('');

  const loadAdminReference = useCallback(async (force = false) => {
    setReferenceRefreshing(true);
    try {
      const [loadedSpec, loadedActions, loadedReadiness, machinesData, statusData] = await Promise.all([
        getCachedJson<AdminSpec>('admin-spec', `${API_BASE}/api/admin/spec`, 30 * 60_000, force),
        getCachedJson<{ actions: AdminAction[] }>('admin-actions', `${API_BASE}/api/admin/actions`, 60_000, force),
        getCachedJson<AdminReadiness>('admin-readiness', `${API_BASE}/api/admin/readiness`, 5 * 60_000, force),
        getCachedJson<{ machine_ids: string[] }>('fullrlbl-machines', `${API_BASE}/api/admin/fullrlbl/machines`, 30 * 60_000, force),
        getCachedJson<StatusPayload>('runner-machines', `${API_BASE}/api/backfill/status`, 60_000, force),
      ]);
      setFullrlblMachines(machinesData.machine_ids);
      setRunnerMachines(statusData.snapshot.machines);
      setSpec(loadedSpec);
      setActions(loadedActions.actions);
      const pendingTrigger = loadedActions.actions.find((action) =>
        action.action === 'trigger' && ['queued', 'validating', 'preparing_manifests', 'submitting'].includes(action.phase ?? ''),
      );
      if (pendingTrigger) setTriggerSubmission(pendingTrigger);
      setAdminReadiness(loadedReadiness);
      setSource((current) => ({
        ...current,
        local_flow_path: current.local_flow_path || loadedSpec.default_local_flow_path,
      }));
      setTrigger((current) => ({
        ...current,
        namespace: current.namespace || loadedSpec.default_dev_namespace,
      }));
      setStop((current) => ({
        ...current,
        namespace: current.namespace || loadedSpec.default_dev_namespace,
      }));
      setAdminError(null);
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : String(error));
    } finally {
      setAdminLoading(false);
      setReferenceRefreshing(false);
    }
  }, []);

  const applyWorkflowSummary = useCallback((summary: WorkflowSummaryPayload) => {
    setWorkflowSummary(summary);
    setRunningWorkflows({
      ...summary,
      workflows: summary.workflows.filter((workflow) => !['succeeded', 'completed', 'failed', 'error'].includes(workflow.status.toLowerCase())),
    });
  }, []);

  const loadWorkflowData = useCallback(async (force = false) => {
    setWorkflowRefreshing(true);
    try {
      const summary = await getCachedJson<WorkflowSummaryPayload>('admin-workflow-summary', `${API_BASE}/api/admin/workflows/summary`, 2 * 60_000, force);
      applyWorkflowSummary(summary);
      setAdminError(null);
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : String(error));
    } finally {
      setWorkflowRefreshing(false);
    }
  }, [applyWorkflowSummary]);

  const loadAdmin = useCallback(async () => {
    await Promise.all([loadAdminReference(true), loadWorkflowData(true)]);
  }, [loadAdminReference, loadWorkflowData]);

  useEffect(() => {
    void Promise.all([loadAdminReference(), loadWorkflowData()]);
  }, [loadAdminReference, loadWorkflowData]);

  // Batch busy-machine check: combines control-store records with Argo running
  // workflows so externally-started backfills are detected too. The result is
  // cached in sessionStorage; the refresh button bypasses the cache.
  const loadRunnerBusyStatus = useCallback(async (force = false) => {
    setRunnerBusyLoading(true);
    try {
      const data = await getCachedJson<{ busy_machines: Record<string, BusyMachineInfo> }>(
        'runner-busy-status', `${API_BASE}/api/backfill/runner-lock-status`, 60_000, force,
      );
      setRunnerBusyMachines(data.busy_machines);
    } catch {
      setRunnerBusyMachines({});
    } finally {
      setRunnerBusyLoading(false);
    }
  }, []);

  useEffect(() => {
    const actionId = triggerSubmission?.id;
    if (!actionId || !['queued', 'validating', 'preparing_manifests', 'submitting'].includes(triggerSubmission?.phase ?? '')) return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const response = await fetch(`${API_BASE}/api/admin/actions/${encodeURIComponent(actionId)}`, { cache: 'no-store' });
        const payload = await response.json() as { action?: AdminAction; detail?: string };
        if (!response.ok || !payload.action) throw new Error(payload.detail ?? `Could not refresh trigger status (${response.status})`);
        if (cancelled) return;
        const action = payload.action;
        setTriggerSubmission(action);
        const isTerminal = ['accepted', 'failed'].includes(action.phase ?? '') || ['failed', 'succeeded'].includes(action.status);
        if (isTerminal) {
          if (action.phase === 'failed' || action.status === 'failed') setAdminError(action.error ?? 'Backfill submission failed.');
          if (refreshedTriggerActionId.current !== actionId) {
            refreshedTriggerActionId.current = actionId;
            void loadAdmin();
          }
          return;
        }
      } catch (error) {
        if (!cancelled) setAdminError(error instanceof Error ? error.message : String(error));
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 1000);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [triggerSubmission?.id, triggerSubmission?.phase, triggerSubmission?.status, loadAdmin]);

  // The machine picker is also used by the older forms below, so obtain the
  // authoritative per-machine lock state once for the whole admin view.
  useEffect(() => {
    let cancelled = false;
    const loadActiveMonths = async () => {
      if (runnerMachines.length === 0) {
        setRunnerActiveMonths({});
        return;
      }
      setRunnerActiveMonthsLoading(true);
      const entries = await Promise.all(runnerMachines.map(async (machine) => {
        try {
          const response = await fetch(`${API_BASE}/api/machines/${encodeURIComponent(machine.machine_id)}/backfills/active`);
          if (!response.ok) return [machine.machine_id, []] as const;
          const payload = await response.json() as { months?: ActiveMonth[] };
          return [machine.machine_id, payload.months ?? []] as const;
        } catch {
          return [machine.machine_id, []] as const;
        }
      }));
      if (!cancelled) {
        setRunnerActiveMonths(Object.fromEntries(entries));
        setRunnerActiveMonthsLoading(false);
      }
    };
    void loadActiveMonths();
    return () => { cancelled = true; };
  }, [runnerMachines]);

  // Load batch busy status when runner machines are loaded.
  useEffect(() => {
    if (runnerMachines.length > 0) void loadRunnerBusyStatus();
  }, [runnerMachines, loadRunnerBusyStatus]);

  // Readiness is cached for the overview, but a stale failed check must not
  // leave the operational runner disabled after the platform has recovered.
  // Revalidate both platform readiness and machine ownership whenever the
  // operator opens the Backfill runner.
  useEffect(() => {
    if (adminView !== 'backfill') return;
    void Promise.all([loadAdminReference(true), loadRunnerBusyStatus(true)]);
  }, [adminView, loadAdminReference, loadRunnerBusyStatus]);

  useEffect(() => {
    if (adminView !== 'operations' || !workflowMutationsEnabled) return;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let closedByViewChange = false;
    const connect = () => {
      socket = new WebSocket(workflowSocketUrl());
      socket.onmessage = (event) => {
        try {
          const summary = JSON.parse(event.data) as WorkflowSummaryPayload;
          writeCachedJson('admin-workflow-summary', summary);
          applyWorkflowSummary(summary);
          setAdminError(null);
        } catch {
          setAdminError('Live workflow update could not be read.');
        }
      };
      socket.onclose = () => {
        if (!closedByViewChange) reconnectTimer = window.setTimeout(connect, 3_000);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    return () => {
      closedByViewChange = true;
      window.clearTimeout(reconnectTimer);
      socket?.close();
    };
  }, [adminView, applyWorkflowSummary, workflowMutationsEnabled]);

  const submitAdmin = async (path: string, body: object, label = path) => {
    if (!workflowMutationsEnabled && path !== '/api/admin/manifests/machine' && path !== '/api/admin/manifests/multiple' && path !== '/api/admin/manifests/orchestrated') {
      setAdminError('Workflow operations are disabled in monitor-only runtime.');
      return;
    }
    const isQueuedBackfillTrigger = path === '/api/admin/backfills/orchestrated' || path === '/api/admin/backfills/all';
    if (isQueuedBackfillTrigger) setTriggerSubmitting(true);
    setBusyAction(label);
    try {
      const response = await fetch(`${API_BASE}${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.detail ?? `Request failed: ${response.status}`);
      }
      if (isQueuedBackfillTrigger) {
        const action = (result as { action?: AdminAction }).action;
        if (!action?.id) throw new Error('The server accepted no backfill action record. Refresh the admin actions to verify submission state.');
        refreshedTriggerActionId.current = null;
        setTriggerSubmission(action);
        setAdminError(null);
        onToast('info', `Backfill request accepted (${action.id.slice(0, 8)}); preparation is starting.`);
        return;
      }
      await loadAdmin();
      setAdminError(null);
      onToast('success', `${label} completed successfully.`);
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      setAdminError(msg);
      onToast('error', `${label} failed: ${msg}`);
    } finally {
      setBusyAction(null);
      if (isQueuedBackfillTrigger) setTriggerSubmitting(false);
    }
  };

  const setWorkflowReviewed = async (workflow: RunningWorkflow, reviewed: boolean) => {
    const label = reviewed ? `review-${workflow.workflow_id}` : `unreview-${workflow.workflow_id}`;
    setBusyAction(label);
    try {
      const response = await fetch(`${API_BASE}/api/admin/workflows/${encodeURIComponent(workflow.workflow_id)}/review`, {
        method: reviewed ? 'POST' : 'DELETE',
      });
      const result = await response.json() as { detail?: string };
      if (!response.ok) throw new Error(result.detail ?? `Request failed: ${response.status}`);
      const reviewedWorkflowIds = (result as { reviewed_workflow_ids?: string[] }).reviewed_workflow_ids;
      setWorkflowSummary((current) => {
        if (!current) return current;
        const next = { ...current, reviewed_workflow_ids: reviewedWorkflowIds ?? current.reviewed_workflow_ids };
        writeCachedJson('admin-workflow-summary', next);
        return next;
      });
      onToast('success', reviewed ? 'Failure marked as reviewed.' : 'Failure returned to the review queue.');
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setAdminError(message);
      onToast('error', `Unable to update review status: ${message}`);
    } finally {
      setBusyAction(null);
    }
  };

  const createMachineManifest = async (): Promise<string | null> => {
    setBusyAction('manifest');
    try {
      const response = await fetch(`${API_BASE}/api/admin/manifests/machine`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(manifest),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.detail ?? `Request failed: ${response.status}`);
      }
      setTrigger((current) => ({
        ...current,
        storage_account_manifest_path: result.manifest.manifest_path,
      }));
      await loadAdmin();
      setAdminError(null);
      return result.manifest.manifest_path;
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : String(error));
      return null;
    } finally {
      setBusyAction(null);
    }
  };

  const createMultiMachineManifest = async () => {
    setBusyAction('multi-manifest');
    try {
      const response = await fetch(`${API_BASE}/api/admin/manifests/multiple`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...multiManifest,
          machine_ids: parseMachineIds(multiManifest.machine_ids),
        }),
      });
      const result = await response.json() as { manifest?: ManifestResult; detail?: string };
      if (!response.ok || !result.manifest) {
        throw new Error(result.detail ?? `Request failed: ${response.status}`);
      }
      setTrigger((current) => ({ ...current, storage_account_manifest_path: result.manifest!.manifest_path }));
      setCreatedManifest(result.manifest);
      await loadAdmin();
      setAdminError(null);
      onToast('success', `Created manifest for ${result.manifest.rows} machines.`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setAdminError(message);
      onToast('error', `Manifest creation failed: ${message}`);
    } finally {
      setBusyAction(null);
    }
  };

  const createManifestAndTrigger = async (runnerSelection = false) => {
    if (selectedMachineIdsHaveActiveBackfill([manifest.machine_id])) {
      onToast('error', 'This machine already has an active backfill. Stop it or wait for it to finish before starting another.');
      return;
    }
    await submitAdmin('/api/admin/backfills/orchestrated', {
      machine_ids: runnerSelection ? Object.keys(runnerMonthPlan) : [manifest.machine_id],
      since: manifest.since,
      until: manifest.until,
      manifest_prefix: manifest.manifest_path,
      month_indices_by_machine: runnerSelection ? runnerMonthPlan : {},
      params: buildTriggerParams({ ...trigger, max_parallel_steps: 1 }, productionConfirmation),
    }, 'ULRPM orchestrator');
  };

  const createMultiManifestAndTrigger = async (runnerSelection = false) => {
    const machineIds = runnerSelection ? Object.keys(runnerMonthPlan) : parseMachineIds(multiManifest.machine_ids);
    if (machineIds.length === 0) {
      onToast('error', 'Add at least one machine ID before starting a scoped backfill.');
      return;
    }
    if (selectedMachineIdsHaveActiveBackfill(machineIds)) {
      onToast('error', 'At least one selected machine already has an active backfill. Remove it from this run, or stop/wait for its current backfill.');
      return;
    }
    await submitAdmin('/api/admin/backfills/orchestrated', {
      machine_ids: machineIds,
      since: multiManifest.since,
      until: multiManifest.until,
      manifest_prefix: multiManifest.manifest_path,
      month_indices_by_machine: runnerSelection ? runnerMonthPlan : {},
      params: buildTriggerParams({ ...trigger, max_parallel_steps: runnerSelection ? runnerConcurrency : trigger.max_parallel_steps }, productionConfirmation),
    }, `scoped backfill for ${machineIds.length} machine${machineIds.length === 1 ? '' : 's'}`);
  };

  const createRunnerManifests = async () => {
    const plannedMachineIds = Object.keys(runnerMonthPlan);
    if (!plannedMachineIds.length) {
      onToast('error', 'Select at least one machine before creating manifests.');
      return;
    }
    setBusyAction('runner-manifest');
    try {
      const manifestPrefix = backfillScope === 'one' ? manifest.manifest_path : backfillScope === 'selected' ? multiManifest.manifest_path : allMachines.manifest_path;
      const response = await fetch(`${API_BASE}/api/admin/manifests/orchestrated`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          machine_ids: plannedMachineIds,
          since: runnerSince,
          until: runnerUntil,
          manifest_prefix: manifestPrefix,
          month_indices_by_machine: runnerMonthPlan,
        }),
      });
      const result = await response.json() as { manifest?: ManifestResult; machine_count?: number; detail?: string };
      if (!response.ok || !result.manifest) throw new Error(result.detail ?? `Request failed: ${response.status}`);
      const createdPrefix = result.manifest.manifest_prefix ?? manifestPrefix;
      if (backfillScope === 'one') setManifest((current) => ({ ...current, manifest_path: createdPrefix }));
      else if (backfillScope === 'selected') setMultiManifest((current) => ({ ...current, manifest_path: createdPrefix }));
      else setAllMachines((current) => ({ ...current, manifest_path: createdPrefix }));
      setCreatedManifest(result.manifest);
      await loadAdmin();
      setAdminError(null);
      onToast('success', `Created ${result.manifest.rows} monthly manifest${result.manifest.rows === 1 ? '' : 's'} for ${result.machine_count ?? plannedMachineIds.length} machine${(result.machine_count ?? plannedMachineIds.length) === 1 ? '' : 's'} without starting a backfill.`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setAdminError(message);
      onToast('error', `Manifest creation failed: ${message}`);
    } finally {
      setBusyAction(null);
    }
  };

  const selectRunnerMonths = async () => {
    setClearedRunnerPlanKey('');
    await loadAdminReference(true);
  };

  const createAllMachinesManifestAndTrigger = async (runnerSelection = false) => {
    const targetMachineIds = runnerSelection ? Object.keys(runnerMonthPlan) : runnerMachines.map((machine) => machine.machine_id);
    if (runnerSelection && targetMachineIds.length === 0) {
      onToast('error', 'There are no online, backfillable months in the current selection.');
      return;
    }
    if (targetMachineIds.some((machineId) => runnerMachineIsLocked(machineId))) {
      onToast('error', 'One or more inventory machines already have an active backfill. The all-machines run cannot start until they finish or are stopped.');
      return;
    }
    await submitAdmin(
      runnerSelection ? '/api/admin/backfills/orchestrated' : '/api/admin/backfills/all',
      {
        ...(runnerSelection ? {} : { source }),
        params: buildTriggerParams({ ...trigger, max_parallel_steps: runnerSelection ? runnerConcurrency : trigger.max_parallel_steps }, productionConfirmation),
        ...(runnerSelection ? {
          machine_ids: targetMachineIds,
          since: runnerSince,
          until: runnerUntil,
          manifest_prefix: allMachines.manifest_path,
        } : allMachines),
        month_indices_by_machine: runnerSelection ? runnerMonthPlan : {},
      },
      'All-machines backfill',
    );
  };

  const triggerFullRlblTest = async () => {
    setBusyAction('fullrlbl-trigger');
    try {
      const machineIdList = fullrlblForm.machine_ids.trim()
        ? parseMachineIds(fullrlblForm.machine_ids)
        : fullrlblMachines;
      const response = await fetch(`${API_BASE}/api/admin/fullrlbl/trigger`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...fullrlblForm,
          machine_ids: machineIdList,
        }),
      });
      const result = await response.json() as { detail?: string; machine_count?: number; test_mode?: string };
      if (!response.ok) {
        throw new Error(result.detail ?? `Request failed: ${response.status}`);
      }
      await loadAdmin();
      setAdminError(null);
      onToast('success', `FullRlbl test queued for ${result.machine_count} machines (${result.test_mode}).`);
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      setAdminError(msg);
      onToast('error', `FullRlbl test failed: ${msg}`);
    } finally {
      setBusyAction(null);
    }
  };

  const runningUrl = spec?.outerbounds_running_url ?? `${DEFAULT_OUTERBOUNDS_URL}&status=running`;
  const pastUrl = spec?.outerbounds_past_url ?? `${DEFAULT_OUTERBOUNDS_URL}&status=completed,failed,error`;
  const workflows = runningWorkflows?.workflows ?? [];
  const manifestStorageReady = adminReadiness?.manifest_storage.ready === true;
  const argoReady = adminReadiness?.argo.ready === true;
  const fullrlblReady = adminReadiness?.fullrlbl_test?.ready === true;
  const readyChecks = [manifestStorageReady, argoReady, fullrlblReady].filter(Boolean).length;
  const summaryCounts = workflowSummary?.counts;
  const activeRuns = summaryCounts?.active ?? 0;
  const completedRuns = summaryCounts?.completed ?? 0;
  const reviewedWorkflowIds = new Set(workflowSummary?.reviewed_workflow_ids ?? []);
  const workflowSortTime = (workflow: RunningWorkflow) => Date.parse(workflow.finished_at ?? workflow.started_at ?? workflow.created_at ?? '') || 0;
  const completedWorkflows = (workflowSummary?.workflows ?? []).filter((workflow) => ['succeeded', 'completed'].includes(workflow.status.toLowerCase())).sort((left, right) => workflowSortTime(right) - workflowSortTime(left));
  const failedWorkflows = (workflowSummary?.workflows ?? []).filter((workflow) => ['failed', 'error'].includes(workflow.status.toLowerCase())).sort((left, right) => workflowSortTime(right) - workflowSortTime(left));
  const unreviewedFailedWorkflows = failedWorkflows.filter((workflow) => !reviewedWorkflowIds.has(workflow.workflow_id));
  const reviewedFailedWorkflows = failedWorkflows.filter((workflow) => reviewedWorkflowIds.has(workflow.workflow_id));
  const unreviewedFailedRuns = unreviewedFailedWorkflows.length;
  const overviewLoading = workflowSummary === null;
  const overviewUnavailable = Boolean(workflowSummary?.error || workflowSummary?.argo_available === false);
  const exceedsRecommendedConcurrency = parallelMachineRuns && trigger.max_parallel_steps > RECOMMENDED_MACHINE_CONCURRENCY;
  const workflowGroups = Object.entries(
    workflows.reduce<Record<string, RunningWorkflow[]>>((groups, workflow) => {
      const flowName = workflow.flow_name || 'FSTBackfill';
      (groups[flowName] ??= []).push(workflow);
      return groups;
    }, {}),
  ).sort(([left], [right]) => left.localeCompare(right));
  const selectedRunnerMachineIds = backfillScope === 'one'
    ? (manifest.machine_id ? [manifest.machine_id] : [])
    : backfillScope === 'selected' ? parseMachineIds(multiManifest.machine_ids) : runnerMachines.map((machine) => machine.machine_id);
  const runnerSince = backfillScope === 'one' ? manifest.since : backfillScope === 'selected' ? multiManifest.since : allMachines.since;
  const runnerUntil = backfillScope === 'one' ? manifest.until : backfillScope === 'selected' ? multiManifest.until : allMachines.until;
  const computedRunnerMonthSelection = buildRunnerMonthSelection(
    runnerMachines, selectedRunnerMachineIds, runnerMonthMode, runnerSince, runnerUntil, manualMonthIndices,
  );
  const computedRunnerMonthPlanKey = JSON.stringify({
    scope: backfillScope, mode: runnerMonthMode, machineIds: selectedRunnerMachineIds,
    since: runnerSince, until: runnerUntil, manualMonthIndices, plan: computedRunnerMonthSelection.plan,
  });
  const runnerPlanWasCleared = clearedRunnerPlanKey === computedRunnerMonthPlanKey;
  const runnerMonthSelection = runnerPlanWasCleared
    ? { ...computedRunnerMonthSelection, plan: {}, skippedCount: computedRunnerMonthSelection.candidateCount }
    : computedRunnerMonthSelection;
  const runnerMonthPlan = runnerMonthSelection.plan;
  const plannedRunnerMachineCount = Object.keys(runnerMonthPlan).length;
  const runnerConcurrencyMax = Math.max(1, plannedRunnerMachineCount);
  const runnerConcurrency = backfillScope === 'one' || !parallelMachineRuns
    ? 1
    : Math.min(runnerConcurrencyMax, Math.max(1, Math.floor(Number(trigger.max_parallel_steps) || 1)));
  const runnerMonthAvailabilityKey = JSON.stringify(runnerMonthSelection.availability);
  useEffect(() => {
    setTrigger((current) => {
      const nextValue = backfillScope === 'one' || !parallelMachineRuns
        ? 1
        : Math.min(runnerConcurrencyMax, Math.max(1, Math.floor(Number(current.max_parallel_steps) || 1)));
      return current.max_parallel_steps === nextValue ? current : { ...current, max_parallel_steps: nextValue };
    });
  }, [backfillScope, parallelMachineRuns, runnerConcurrencyMax]);
  useEffect(() => {
    const availableIndices = new Set(Object.entries(runnerMonthSelection.availability).filter(([, count]) => count > 0).map(([index]) => Number(index)));
    setManualMonthIndices((current) => current.filter((index) => availableIndices.has(index)));
  }, [runnerMonthAvailabilityKey]);
  const visibleRunnerMachines = runnerMachines.filter((machine) => machine.machine_id.toLowerCase().includes(runnerMachineQuery.trim().toLowerCase()));
  const runnerMachinesLoading = adminLoading && runnerMachines.length === 0;
  const runnerLockStatusLoading = runnerMachinesLoading || runnerActiveMonthsLoading || runnerBusyLoading;
  const activeRunnerMonths = (machineId: string) => (runnerActiveMonths[machineId] ?? []).filter((month) => ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'].includes(month.state));
  const runnerMachineIsBusy = (machineId: string) => machineId in runnerBusyMachines;
  const runnerMachineIsLocked = (machineId: string) => activeRunnerMonths(machineId).length > 0 || runnerMachineIsBusy(machineId);
  const runnerBusyLabel = (machineId: string): string => {
    const info = runnerBusyMachines[machineId];
    if (!info) return '';
    if (info.source === 'argo_running') return `Running workflow: ${info.workflow_id ?? 'unknown'}`;
    return activeRunnerMonths(machineId).map((m) => `${m.year}-${String(m.month).padStart(2, '0')}`).join(', ') || 'Active backfill';
  };
  const scopedRunnerHasActiveBackfill = selectedRunnerMachineIds.some(runnerMachineIsLocked);
  const selectableVisibleRunnerMachines = runnerLockStatusLoading ? [] : visibleRunnerMachines.filter((machine) => !runnerMachineIsLocked(machine.machine_id));
  const selectedMachineIdsHaveActiveBackfill = (machineIds: string[]) => machineIds.some(runnerMachineIsLocked);
  const legacyOneMachineLocked = Boolean(manifest.machine_id) && runnerMachineIsLocked(manifest.machine_id);
  const legacySelectedMachinesLocked = selectedMachineIdsHaveActiveBackfill(parseMachineIds(multiManifest.machine_ids));
  const inventoryHasActiveBackfill = runnerMachines.some((machine) => runnerMachineIsLocked(machine.machine_id));
  const plannedMonthLabels = [...new Set(Object.entries(runnerMonthPlan).flatMap(([machineId, indices]) => {
    const machine = runnerMachines.find((item) => item.machine_id === machineId);
    return machine?.months.filter((month) => indices.includes(orchestratorMonthIndex(month.partition))).map((month) => month.partition.label) ?? [];
  }))].sort();
  const plannedPartitionCount = Object.values(runnerMonthPlan).reduce((sum, months) => sum + months.length, 0);
  const setRunnerRange = (field: 'since' | 'until', value: string) => {
    const normalized = field === 'until' && value === utcToday() ? new Date().toISOString() : value;
    if (backfillScope === 'one') setManifest({ ...manifest, [field]: normalized });
    else if (backfillScope === 'selected') setMultiManifest({ ...multiManifest, [field]: normalized });
    else setAllMachines({ ...allMachines, [field]: normalized });
  };
  const rangePartitionCount = runnerMonthMode === 'range' ? plannedPartitionCount : 0;
  const productionConfirmation = spec?.prod_confirmation ?? PROD_CONFIRMATION;
  const productionConfirmed = trigger.environment !== 'prod' || trigger.confirm_production;
  const runnerSubmitBlockers = [
    ...(!workflowMutationsEnabled ? ['Workflow operations are disabled in monitor-only runtime.'] : []),
    ...(busyAction !== null ? ['Another admin action is still in progress.'] : []),
    ...(adminReadiness === null ? ['Platform readiness checks are still loading.'] : []),
    ...(!manifestStorageReady ? [adminReadiness?.manifest_storage.detail ?? 'Manifest storage is not ready.'] : []),
    ...(!argoReady ? [adminReadiness?.argo.detail ?? 'Workflow actions are not ready.'] : []),
    ...(runnerLockStatusLoading ? ['Machine backfill status is still loading.'] : []),
    ...(selectedRunnerMachineIds.length === 0 ? ['Select at least one machine.'] : []),
    ...(plannedPartitionCount === 0 ? ['Select at least one online, backfillable month.'] : []),
    ...(scopedRunnerHasActiveBackfill ? ['A selected machine already has an active backfill.'] : []),
    ...(!productionConfirmed ? ['Check the production confirmation checkbox to enable this run.'] : []),
  ];
  const runnerSubmitDisabled = runnerSubmitBlockers.length > 0;
  const triggerSubmissionIsTerminal = Boolean(triggerSubmission && (
    ['accepted', 'failed'].includes(triggerSubmission.phase ?? '')
    || ['failed', 'succeeded'].includes(triggerSubmission.status)
  ));
  const triggerSubmissionActive = triggerSubmitting || Boolean(triggerSubmission && !triggerSubmissionIsTerminal);
  const toggleRunnerMachine = (machineId: string, checked: boolean) => {
    if (backfillScope === 'all') return;
    if (checked && runnerMachineIsLocked(machineId)) {
      onToast('error', 'This machine already has an active backfill. Stop it or wait for it to finish before starting another.');
      return;
    }
    if (backfillScope === 'one') { setManifest({ ...manifest, machine_id: checked ? machineId : '' }); return; }
    const next = checked ? [...new Set([...selectedRunnerMachineIds, machineId])] : selectedRunnerMachineIds.filter((id) => id !== machineId);
    setMultiManifest({ ...multiManifest, machine_ids: next.join('\n') });
  };

  const selectAdminView = (view: AdminView) => {
    setAdminView(view);
    if (view === 'advanced' || view === 'observability') setShowAdvanced(true);
  };

  return (
    <>
    {triggerSubmitting || triggerSubmission ? <section className={`trigger-progress-banner${triggerSubmission?.phase === 'failed' || triggerSubmission?.status === 'failed' ? ' failed' : triggerSubmissionIsTerminal ? ' complete' : ''}`} role="status" aria-live="polite">
      <div className="trigger-progress-icon">{triggerSubmissionActive ? <Loader2 className="spin" size={18} /> : triggerSubmission?.phase === 'failed' || triggerSubmission?.status === 'failed' ? <ShieldAlert size={18} /> : <CheckCircle2 size={18} />}</div>
      <div className="trigger-progress-copy">
        <strong>{triggerSubmitting ? 'Submitting backfill request…' : `Backfill ${triggerPhaseLabel(triggerSubmission?.phase)}`}</strong>
        <span>{triggerSubmission?.id ? `Action ${triggerSubmission.id}${triggerSubmission.machine_ids?.length ? ` · ${triggerSubmission.machine_ids.length} machine${triggerSubmission.machine_ids.length === 1 ? '' : 's'}` : ''}` : 'The server is accepting the request.'}</span>
        {triggerSubmission?.error ? <span className="error-text">{triggerSubmission.error}</span> : null}
      </div>
      {triggerSubmissionIsTerminal ? <button type="button" className="icon-button" onClick={() => setTriggerSubmission(null)} aria-label="Dismiss backfill submission status" title="Dismiss status"><X size={16} /></button> : <span className="trigger-progress-phase">{triggerSubmission?.phase ?? 'sending'}</span>}
    </section> : null}
    <div className={`admin-workspace${adminSidebarOpen ? '' : ' sidebar-collapsed'}`} data-admin-view={adminView} inert={triggerSubmissionActive ? true : undefined}>
      <aside className="admin-sidebar" aria-label="Admin categories">
        <div className="admin-sidebar-brand"><Terminal size={18} /><span>Admin workspace</span><button className="admin-sidebar-toggle" type="button" onClick={() => setAdminSidebarOpen((open) => !open)} title={adminSidebarOpen ? 'Hide admin sidebar' : 'Show admin sidebar'} aria-label={adminSidebarOpen ? 'Hide admin sidebar' : 'Show admin sidebar'}>{adminSidebarOpen ? <ChevronLeft size={17} /> : <ChevronRight size={17} />}</button></div>
        <nav className="admin-sidebar-nav">
          <button className={adminView === 'overview' ? 'active' : ''} type="button" onClick={() => selectAdminView('overview')}><Database size={17} /><span>Overview</span></button>
          <button className={adminView === 'backfill' ? 'active' : ''} type="button" onClick={() => selectAdminView('backfill')}><Play size={17} /><span>Backfill runner</span></button>
          <button className={adminView === 'campaigns' ? 'active' : ''} type="button" onClick={() => selectAdminView('campaigns')}><Database size={17} /><span>Campaigns</span></button>
          <button className={adminView === 'operations' ? 'active' : ''} type="button" onClick={() => selectAdminView('operations')}><Zap size={17} /><span>Live operations</span></button>
          <button className={adminView === 'reviews' ? 'active' : ''} type="button" onClick={() => selectAdminView('reviews')}><CheckCircle2 size={17} /><span>Run reviews{unreviewedFailedRuns ? ` (${unreviewedFailedRuns})` : ''}</span></button>
          <button className={adminView === 'cost' ? 'active' : ''} type="button" onClick={() => selectAdminView('cost')}><History size={17} /><span>Current cost</span></button>
          <button className={adminView === 'advanced' ? 'active' : ''} type="button" onClick={() => selectAdminView('advanced')}><FlaskConical size={17} /><span>Advanced tools</span></button>
          <button className={adminView === 'observability' ? 'active' : ''} type="button" onClick={() => selectAdminView('observability')}><History size={17} /><span>Logs & history</span></button>
        </nav>
      </aside>
      <div className="admin-workspace-content">
      {!workflowMutationsEnabled ? <section className="notice inline-notice runtime-mode-notice" role="status"><ShieldAlert size={17} /><span>Monitor-only mode ({spec?.runtime?.mode ?? 'runtime unknown'}): workflow submissions, stops, and CLI log lookups are disabled. Read-only planning and standalone manifest creation remain available.</span></section> : null}
      <div className="admin-view-panel campaigns-panel"><CampaignWorkspace workflowMutationsEnabled={workflowMutationsEnabled} /></div>
      {adminError ? (
        <section className="notice error">
          <ShieldAlert size={18} />
          <span>{adminError}</span>
        </section>
      ) : null}

      <section className="admin-readiness admin-view-panel overview-panel" aria-live="polite">
        <div className={manifestStorageReady ? 'readiness-item ready' : 'readiness-item blocked'}>
          <strong>Manifest storage</strong>
          <span>{adminReadiness ? (manifestStorageReady ? `Ready via ${adminReadiness.manifest_storage.credential_source === 'key_vault' ? 'Key Vault' : 'configured credential'}` : adminReadiness.manifest_storage.detail) : 'Checking permissions...'}</span>
        </div>
        <div className={argoReady ? 'readiness-item ready' : 'readiness-item blocked'}>
          <strong>Workflow actions</strong>
          <span>{adminReadiness ? (argoReady ? 'Argo authorization verified' : adminReadiness.argo.detail) : 'Checking permissions...'}</span>
        </div>
        <div className={fullrlblReady ? 'readiness-item ready' : 'readiness-item blocked'}>
          <strong>FullRlbl test template</strong>
          <span>{adminReadiness?.fullrlbl_test ? (fullrlblReady ? `Deployed: ${adminReadiness.fullrlbl_test.template_name ?? 'ready'}` : adminReadiness.fullrlbl_test.detail) : 'Checking template...'}</span>
        </div>
      </section>

      {spec?.templates ? (
        <details className="active-templates admin-view-panel overview-panel">
          <summary>Active workflow templates</summary>
          <div className="template-list">
            <div><strong>FSTBackfill:</strong> <code>{spec.templates.fst_backfill}</code></div>
            <div><strong>ULRPM Orchestrator:</strong> <code>{spec.templates.ulrpm_orchestrator}</code></div>
            <div><strong>FullRlbl Test:</strong> <code>{spec.templates.fullrlbl_test}</code></div>
          </div>
        </details>
      ) : null}

      <section className="admin-overview-dashboard admin-view-panel overview-panel">
        <article className="overview-hero">
          <div><p className="eyebrow">Operational overview</p><h2>Backfill control plane</h2><p>Readiness, active workloads, and recent command health in one place.</p></div>
          <div className="overview-hero-actions"><button className="icon-button" type="button" onClick={() => void loadAdmin()} title="Refresh dashboard data" aria-label="Refresh dashboard data" disabled={referenceRefreshing || workflowRefreshing}><RefreshCw className={referenceRefreshing || workflowRefreshing ? 'spin' : ''} size={16} /></button><span className={overviewUnavailable || unreviewedFailedRuns > 0 ? 'overview-health warning' : 'overview-health healthy'}>{overviewLoading ? 'Loading live workflow state…' : overviewUnavailable ? 'Live workflow state unavailable' : unreviewedFailedRuns > 0 ? `${unreviewedFailedRuns} failed run${unreviewedFailedRuns === 1 ? '' : 's'} need review` : 'No failures awaiting review'}</span></div>
        </article>
        <div className="overview-stat-grid">
          <article className="overview-stat"><span>Platform readiness</span><strong>{readyChecks}/3</strong><small>Required services ready</small></article>
          <button className="overview-stat interactive" type="button" onClick={() => selectAdminView('operations')}><span>Active workflows</span><strong>{overviewLoading ? <Loader2 className="spin" size={26} /> : overviewUnavailable ? '—' : activeRuns}</strong><small>Open Live Operations</small></button>
          <button className="overview-stat interactive" type="button" onClick={() => selectAdminView('reviews')}><span>Completed runs</span><strong>{overviewLoading ? <Loader2 className="spin" size={26} /> : overviewUnavailable ? '—' : completedRuns}</strong><small>Review recent runs</small></button>
          <button className="overview-stat interactive" type="button" onClick={() => window.open(spec?.outerbounds_running_url ?? runningUrl, '_blank', 'noopener,noreferrer')}><span>Running / queued</span><strong>{overviewLoading ? <Loader2 className="spin" size={26} /> : overviewUnavailable ? '—' : activeRuns}</strong><small>Open in Outerbounds</small></button>
          <button className="overview-stat interactive failure" type="button" onClick={() => selectAdminView('reviews')}><span>Failed runs</span><strong>{overviewLoading ? <Loader2 className="spin" size={26} /> : overviewUnavailable ? '—' : unreviewedFailedRuns}</strong><small>{unreviewedFailedRuns ? `${unreviewedFailedRuns} awaiting review` : 'All reviewed'}</small></button>
        </div>
        <article className="overview-chart">
          <div className="overview-chart-head"><div><h3>Outerbounds workflow health</h3><p>Live Argo workflow phases from the cluster behind Outerbounds.</p></div><span>{overviewLoading ? 'Loading…' : `${workflowSummary?.workflows.length ?? 0} recent runs`}</span></div>
          <div className="health-bars" aria-label="Recent command health chart">
            <div><span>Completed</span><i><b className="success" style={{ width: `${workflowSummary?.workflows.length ? (completedRuns / workflowSummary.workflows.length) * 100 : 0}%` }} /></i><strong>{overviewLoading ? '—' : completedRuns}</strong></div>
            <div><span>Running / queued</span><i><b className="info" style={{ width: `${workflowSummary?.workflows.length ? (activeRuns / workflowSummary.workflows.length) * 100 : 0}%` }} /></i><strong>{overviewLoading ? '—' : activeRuns}</strong></div>
            <div><span>Failed</span><i><b className="error" style={{ width: `${workflowSummary?.workflows.length ? (unreviewedFailedRuns / workflowSummary.workflows.length) * 100 : 0}%` }} /></i><strong>{overviewLoading ? '—' : unreviewedFailedRuns}</strong></div>
          </div>
        </article>
        <div className="overview-bottom-grid">
          <article className="overview-quick-actions"><h3>Quick actions</h3><p>Common operator workflows.</p><div><button className="primary-button" type="button" onClick={() => selectAdminView('backfill')}><Play size={16} />Start a backfill</button><button className="secondary-button" type="button" onClick={() => selectAdminView('operations')}><Zap size={16} />View active runs</button><a className="secondary-button" href={runningUrl} target="_blank" rel="noreferrer"><ExternalLink size={16} />Open Outerbounds</a></div></article>
          <article className="operator-note"><div className="overview-chart-head"><div><h3>Operator handoff</h3><p>Keep the next shift’s context here.</p></div><button className="icon-button" type="button" onClick={() => localStorage.setItem('ulrpm-admin-operator-note', operatorNote)} title="Save note"><Check size={16} /></button></div><textarea value={operatorNote} onChange={(event) => setOperatorNote(event.target.value)} placeholder="Add a note, run context, or handoff detail…" rows={4} /></article>
        </div>
      </section>

      <CurrentCostPanel />

      <section className="run-review-panel admin-view-panel reviews-panel">
        <header className="run-review-header">
          <div><p className="eyebrow">Run review</p><h2>Completed and failed workflows</h2><p>Review recent outcomes from Outerbounds. Marking a failed run as reviewed clears it from the operational attention message; it never alters the workflow itself.</p></div>
          <a className="secondary-button" href={pastUrl} target="_blank" rel="noreferrer"><ExternalLink size={16} />Open in Outerbounds</a>
        </header>
        {overviewLoading ? <LoadingWidget label="Loading recent workflow outcomes" /> : overviewUnavailable ? <div className="empty-state">Recent workflow state is unavailable. Open Outerbounds to inspect past runs.</div> : <>
          <div className="run-review-columns">
            <section className="run-review-list completed"><div className="run-review-list-heading"><div><span>Completed</span><h3>{completedWorkflows.length} recent successful run{completedWorkflows.length === 1 ? '' : 's'}</h3></div></div>
              {completedWorkflows.map((workflow) => <article className="run-review-row" key={workflow.workflow_id}><div><strong className="mono break">{workflow.workflow_id}</strong><div className="run-review-metadata"><span><b>Flow</b>{workflow.flow_name}</span>{workflow.branch ? <span><b>Branch</b>{workflow.branch}</span> : null}<span><b>Namespace</b>{workflow.namespace}</span><span><b>Finished</b>{formatTimestamp(workflow.finished_at ?? workflow.started_at ?? workflow.created_at)}</span></div></div><a className="icon-link" href={workflow.outerbounds_url} target="_blank" rel="noreferrer" title="Open in Outerbounds" aria-label="Open in Outerbounds"><ExternalLink size={16} /></a></article>)}
              {completedWorkflows.length === 0 ? <p className="empty-state">No completed workflows in the recent Argo history.</p> : null}
            </section>
            <section className="run-review-list failed"><div className="run-review-list-heading"><div><span>Failed / errored</span><h3>{unreviewedFailedWorkflows.length} run{unreviewedFailedWorkflows.length === 1 ? '' : 's'} awaiting review</h3></div><small>{unreviewedFailedRuns} awaiting review</small></div>
              {unreviewedFailedWorkflows.map((workflow) => { const reviewAction = `review-${workflow.workflow_id}`; return <article className="run-review-row" key={workflow.workflow_id}><div><strong className="mono break">{workflow.workflow_id}</strong><div className="run-review-metadata"><span><b>Flow</b>{workflow.flow_name}</span>{workflow.branch ? <span><b>Branch</b>{workflow.branch}</span> : null}<span><b>Namespace</b>{workflow.namespace}</span><span><b>Phase</b>{workflow.status}</span><span><b>Finished</b>{formatTimestamp(workflow.finished_at ?? workflow.started_at ?? workflow.created_at)}</span></div></div><div className="actions"><a className="icon-link" href={workflow.outerbounds_url} target="_blank" rel="noreferrer" title="Open in Outerbounds" aria-label="Open in Outerbounds"><ExternalLink size={16} /></a><button className="review-action-button mark-reviewed" type="button" onClick={() => void setWorkflowReviewed(workflow, true)} disabled={busyAction === reviewAction}>{busyAction === reviewAction ? <Loader2 className="spin" size={15} /> : <Check size={15} />}<span>Mark reviewed</span></button></div></article>; })}
              {unreviewedFailedWorkflows.length === 0 ? <p className="empty-state">No failed workflows awaiting review.</p> : null}
            </section>
          </div>
          {reviewedFailedWorkflows.length ? <details className="reviewed-runs"><summary>Reviewed runs <span>{reviewedFailedWorkflows.length}</span></summary><div>{reviewedFailedWorkflows.map((workflow) => { const reviewAction = `unreview-${workflow.workflow_id}`; return <article className="run-review-row reviewed" key={workflow.workflow_id}><div><strong className="mono break">{workflow.workflow_id}</strong><div className="run-review-metadata"><span><b>Flow</b>{workflow.flow_name}</span><span><b>Finished</b>{formatTimestamp(workflow.finished_at ?? workflow.started_at ?? workflow.created_at)}</span></div></div><div className="actions"><a className="icon-link" href={workflow.outerbounds_url} target="_blank" rel="noreferrer" title="Open in Outerbounds"><ExternalLink size={16} /></a><button className="review-action-button reviewed-action" type="button" onClick={() => void setWorkflowReviewed(workflow, false)} disabled={busyAction === reviewAction}><Check size={15} /><span>Reviewed</span></button></div></article>; })}</div></details> : null}
        </>}
      </section>

      <section className="admin-intro admin-view-panel backfill-panel">
        <div>
          <p className="eyebrow">Backfill operations</p>
          <h2>Start with a manifest, then run the backfill.</h2>
          <p>Common daily actions are shown below. Workflow setup, FullRlbl testing, logs, and action history are available when needed.</p>
        </div>
      </section>

      <section className="panel backfill-runner-panel admin-view-panel backfill-panel">
        <div className="panel-header">
          <div>
            <p className="eyebrow">Backfill runner</p>
            <h2>Choose the machines, then run one consistent workflow.</h2>
            <p>Machine lanes can run concurrently; each machine’s monthly backfills stay sequential.</p>
          </div>
        </div>
        <div className="backfill-scope-tabs" role="tablist" aria-label="Backfill scope">
          <button className={backfillScope === 'one' ? 'active' : ''} type="button" onClick={() => { setBackfillScope('one'); setParallelMachineRuns(false); setTrigger({ ...trigger, max_parallel_steps: 1 }); }}>Backfill One Machine</button>
          <button className={backfillScope === 'selected' ? 'active' : ''} type="button" onClick={() => setBackfillScope('selected')}>Backfill Selected Machines</button>
          <button className={backfillScope === 'all' ? 'active' : ''} type="button" onClick={() => setBackfillScope('all')}>Backfill Every Inventory Machine</button>
        </div>
        <div className="form-grid backfill-runner-form">
          <label>
            Target
            <select value={trigger.environment} onChange={(event) => setTrigger({ ...trigger, environment: event.target.value as EnvironmentTarget })}>
              <option value="dev">Dev/Test</option>
              <option value="prod">Production</option>
            </select>
          </label>
          <label>
            Container / namespace
            <input value={trigger.environment === 'prod' ? spec?.prod_namespace ?? PROD_CONTAINER : trigger.namespace} disabled={trigger.environment === 'prod'} onChange={(event) => setTrigger({ ...trigger, namespace: event.target.value })} />
          </label>
          {trigger.environment === 'prod' ? (
            <>
              <label className="checkbox-field">
                <input type="checkbox" checked={trigger.confirm_production} onChange={(event) => setTrigger({ ...trigger, confirm_production: event.target.checked })} />
                Confirm production backfill
              </label>
            </>
          ) : null}
          <small className="wide">The same orchestrator template and branch are used for both targets; only its container namespace changes. The child flow's existing is_prod check skips dev seeding in production.</small>
          {backfillScope === 'one' ? (
            <label className="wide">Selected machine
              <input value={manifest.machine_id} readOnly placeholder="Select one machine from the table below" />
            </label>
          ) : backfillScope === 'selected' ? (
            <label className="wide">Selected machines
              <textarea value={multiManifest.machine_ids} readOnly rows={4} placeholder="Select one or more machines from the table below" />
            </label>
          ) : (
            <div className="wide scope-summary"><Database size={18} /><span>Every machine in the latest dashboard inventory will be included.</span></div>
          )}
          <section className="wide runner-machine-picker" aria-label="Machine selection">
            <div className="runner-machine-picker-head"><div><strong>{runnerMachinesLoading ? 'Loading machine inventory…' : backfillScope === 'all' ? `All ${runnerMachines.length} inventory machines included` : `${selectedRunnerMachineIds.length} machine${selectedRunnerMachineIds.length === 1 ? '' : 's'} selected`}</strong><span>Machines with an active backfill are locked to prevent a second parent workflow.</span></div><div className="runner-machine-picker-controls"><button className="icon-button" type="button" onClick={() => void loadRunnerBusyStatus(true)} title="Refresh backfill status for all machines" aria-label="Refresh backfill status" disabled={runnerBusyLoading}><RefreshCw className={runnerBusyLoading ? 'spin' : ''} size={15} /></button><input value={runnerMachineQuery} onChange={(event) => setRunnerMachineQuery(event.target.value)} placeholder="Filter machine ID" aria-label="Filter machines" disabled={runnerMachinesLoading} /></div></div>
            <div className="runner-machine-table"><table><thead><tr><th><input type="checkbox" aria-label="Select all eligible visible machines" checked={selectableVisibleRunnerMachines.length > 0 && selectableVisibleRunnerMachines.every((machine) => selectedRunnerMachineIds.includes(machine.machine_id))} disabled={runnerLockStatusLoading || backfillScope === 'one' || backfillScope === 'all' || selectableVisibleRunnerMachines.length === 0} onChange={(event) => setMultiManifest({ ...multiManifest, machine_ids: (event.target.checked ? selectableVisibleRunnerMachines.map((machine) => machine.machine_id) : []).join('\n') })} /></th><th>Machine</th><th>Status</th><th>Coverage</th><th>Backfill</th></tr></thead><tbody>{runnerMachinesLoading ? <tr className="runner-machine-loading"><td colSpan={5}><div className="runner-machine-loading-content"><Loader2 className="spin" size={20} /><span>Loading machine inventory…</span></div></td></tr> : visibleRunnerMachines.length === 0 ? <tr><td colSpan={5} className="empty-state">No machines match this filter.</td></tr> : visibleRunnerMachines.map((machine) => { const locked = runnerMachineIsLocked(machine.machine_id); const busyTitle = runnerBusyLabel(machine.machine_id); return <tr key={machine.machine_id} className={locked ? 'backfill-active' : undefined}><td><input type="checkbox" checked={selectedRunnerMachineIds.includes(machine.machine_id)} disabled={runnerLockStatusLoading || backfillScope === 'all' || locked} onChange={(event) => toggleRunnerMachine(machine.machine_id, event.target.checked)} aria-label={`Select ${machine.machine_id}`} /></td><td className="mono">{machine.machine_id} <TestMachineBadge isTest={machine.is_test_machine} /></td><td><StatusPill status={machine.status} /></td><td>{machine.months_complete}/{machine.months_expected}</td><td>{runnerLockStatusLoading ? <span className="text-muted">Checking…</span> : locked ? <span className="task-phase-badge failed" title={busyTitle || 'Active backfill'}>Busy</span> : <span className="text-ok">Ready</span>}</td></tr>; })}</tbody></table></div>
          </section>
          <section className="wide month-plan-picker" aria-label="Months to backfill"><div className="month-plan-picker-head"><div><strong>Months to backfill</strong><span>Every plan includes only online months with a backfillable status.</span></div></div><div className="month-plan-modes">{(['gaps', 'range', 'all', 'manual'] as const).map((mode) => <button key={mode} className={runnerMonthMode === mode ? 'active' : ''} type="button" onClick={() => { setRunnerMonthMode(mode); setClearedRunnerPlanKey(''); if (mode === 'gaps' || mode === 'all') void selectRunnerMonths(); }}><strong>{{ gaps: 'Fill detected gaps', range: 'Choose a date range', all: 'All months', manual: 'Pick individual months' }[mode]}</strong><span>{{ gaps: 'Online missing partitions only', range: 'Online partitions intersecting dates', all: 'Online needs-backfill or backfilled months', manual: 'Per-machine online month selection' }[mode]}</span></button>)}</div></section>
          {runnerMonthMode === 'gaps' ? <section className="wide runner-month-feedback"><div><strong>{plannedPartitionCount ? `${plannedPartitionCount} gap partition${plannedPartitionCount === 1 ? '' : 's'} selected` : 'No online gaps selected'}</strong><span>{plannedMonthLabels.length ? plannedMonthLabels.join(', ') : 'Select machines with online gaps, then refresh.'}</span>{runnerMonthSelection.skippedCount ? <span>{runnerMonthSelection.skippedCount} non-online or unavailable machine-months skipped.</span> : null}</div><div className="actions"><button className="secondary-button" type="button" onClick={() => void selectRunnerMonths()}>Refresh gaps</button><button className="secondary-button" type="button" onClick={() => setClearedRunnerPlanKey(computedRunnerMonthPlanKey)} disabled={!plannedPartitionCount}>Clear</button></div></section> : null}
          {runnerMonthMode === 'range' ? <section className="wide runner-range-picker"><div className="runner-range-picker-head"><div><strong>Choose a calendar range</strong><span>Only online/backfillable months intersecting this range are planned.</span></div><strong>{rangePartitionCount} partitions planned</strong></div><div className="runner-date-fields"><label onClick={(event) => event.currentTarget.querySelector('input')?.showPicker()}><span>Start date <CalendarDays size={15} /></span><input type="date" max={utcToday()} value={dateOnlyValue(runnerSince)} onFocus={(event) => event.currentTarget.showPicker()} onChange={(event) => setRunnerRange('since', event.target.value)} aria-label="Start date calendar" /></label><label onClick={(event) => event.currentTarget.querySelector('input')?.showPicker()}><span>End date <CalendarDays size={15} /></span><input type="date" max={utcToday()} value={dateOnlyValue(runnerUntil)} onFocus={(event) => event.currentTarget.showPicker()} onChange={(event) => setRunnerRange('until', event.target.value)} aria-label="End date calendar" /></label></div>{runnerMonthSelection.skippedCount ? <p className="text-muted">{runnerMonthSelection.skippedCount} non-online or unavailable machine-months skipped.</p> : null}<div className="actions"><button className="secondary-button" type="button" onClick={() => setRunnerRange('until', new Date().toISOString())}>Until now</button><button className="secondary-button" type="button" onClick={() => { setRunnerRange('since', ''); setRunnerRange('until', ''); }}>Clear dates</button></div></section> : null}
          {runnerMonthMode === 'all' ? <section className="wide runner-month-feedback"><div><strong>{plannedPartitionCount ? `${plannedPartitionCount} online eligible partitions selected` : 'No online eligible months'}</strong><span>{plannedMonthLabels.length ? plannedMonthLabels.join(', ') : 'Select machines to calculate online months.'}</span>{runnerMonthSelection.skippedCount ? <span>{runnerMonthSelection.skippedCount} non-online or unavailable machine-months skipped.</span> : null}</div><div className="actions"><button className="secondary-button" type="button" onClick={() => void selectRunnerMonths()}>Refresh all months</button><button className="secondary-button" type="button" onClick={() => setClearedRunnerPlanKey(computedRunnerMonthPlanKey)} disabled={!plannedPartitionCount}>Clear</button></div></section> : null}
          {runnerMonthMode === 'manual' ? <RunnerMonthCards selected={manualMonthIndices} availability={runnerMonthSelection.availability} machineCount={selectedRunnerMachineIds.length} skippedCount={runnerMonthSelection.skippedCount} onChange={setManualMonthIndices} /> : null}
          {backfillScope === 'one' ? (
            <div className="scope-summary sequential-summary"><Database size={18} /><span>One machine is processed sequentially. Parallel machine lanes do not apply.</span></div>
          ) : (
            <label className="parallel-control">
              <span className="checkbox-field">
                <input type="checkbox" checked={parallelMachineRuns} disabled={plannedRunnerMachineCount === 0} onChange={(event) => {
                  const enabled = event.target.checked;
                  setParallelMachineRuns(enabled);
                  if (!enabled) setTrigger((current) => ({ ...current, max_parallel_steps: 1 }));
                }} />
                Run machines in parallel
              </span>
              {parallelMachineRuns ? <>
                <input type="number" min={1} max={runnerConcurrencyMax} step={1} value={runnerConcurrency} disabled={plannedRunnerMachineCount === 0} onChange={(event) => {
                  const requested = Math.floor(Number(event.target.value) || 1);
                  setTrigger((current) => ({ ...current, max_parallel_steps: Math.min(runnerConcurrencyMax, Math.max(1, requested)) }));
                }} />
                <small>Maximum concurrent machine lanes: {plannedRunnerMachineCount || 0} selected machines.</small>
              </> : <small>Off: run one machine lane at a time.</small>}
            </label>
          )}
          <label className="wide">Manifest path <span className="optional">(optional)</span>
            <input
              value={backfillScope === 'one' ? manifest.manifest_path : backfillScope === 'selected' ? multiManifest.manifest_path : allMachines.manifest_path}
              onChange={(event) => backfillScope === 'one' ? setManifest({ ...manifest, manifest_path: event.target.value }) : backfillScope === 'selected' ? setMultiManifest({ ...multiManifest, manifest_path: event.target.value }) : setAllMachines({ ...allMachines, manifest_path: event.target.value })}
              placeholder="Auto-generated when empty"
            />
          </label>
        </div>
        {scopedRunnerHasActiveBackfill ? <section className="notice error inline-notice" role="alert"><ShieldAlert size={16} /><span>Selected machine already has an active backfill. Starting another is disabled until it finishes or is stopped.</span></section> : null}
        {runnerSubmitBlockers.length ? <section className="notice inline-notice runner-submit-blockers" role="status"><ShieldAlert size={16} /><div><strong>Run is not ready yet</strong><ul>{runnerSubmitBlockers.map((reason) => <li key={reason}>{reason}</li>)}</ul></div><button className="secondary-button" type="button" disabled={referenceRefreshing || runnerBusyLoading} onClick={() => void Promise.all([loadAdminReference(true), loadRunnerBusyStatus(true)])}>{referenceRefreshing || runnerBusyLoading ? <Loader2 className="spin" size={15} /> : <RefreshCw size={15} />}Run checks again</button></section> : null}
        <div className="actions">
          <button className="secondary-button" type="button" onClick={() => void createRunnerManifests()} disabled={busyAction !== null || !manifestStorageReady || plannedPartitionCount === 0}>
            {busyAction === 'runner-manifest' ? <Loader2 className="spin" size={16} /> : <UploadCloud size={16} />}
            <span>Create Manifest</span>
          </button>
          <button className={backfillScope === 'all' ? 'secondary-button' : 'primary-button'} type="button"
            onClick={() => void (backfillScope === 'one' ? createManifestAndTrigger(true) : backfillScope === 'selected' ? createMultiManifestAndTrigger(true) : createAllMachinesManifestAndTrigger(true))}
            disabled={runnerSubmitDisabled}
            title={runnerSubmitBlockers.join(' ')}>
            {busyAction ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
            <span>{backfillScope === 'one' ? 'Run Backfill for This Machine' : backfillScope === 'selected' ? 'Run Backfill for Selected Machines' : 'Run Backfill for Every Inventory Machine'}</span>
          </button>
        </div>
      </section>

      <section className={`admin-grid ${showAdvanced ? 'advanced-open' : ''}`}>
        <div className="panel machine-manifest-panel legacy-backfill-panel">
          <div className="panel-header">
            <div>
              <h2>Backfill One Machine</h2>
              <p>Create the monthly manifests and start a backfill for one machine only.</p>
            </div>
          </div>
          <div className="form-grid">
            <label className="wide">
              Machine ID
              <input value={manifest.machine_id} onChange={(event) => setManifest({ ...manifest, machine_id: event.target.value })} placeholder="683ec59079fecb5a5a240478" />
            </label>
            <DateRangeFields
              since={manifest.since}
              until={manifest.until}
              onSinceChange={(since) => setManifest({ ...manifest, since })}
              onUntilChange={(until) => setManifest({ ...manifest, until })}
            />
            <label className="wide">
              Monthly manifest directory
              <input value={manifest.manifest_path} onChange={(event) => setManifest({ ...manifest, manifest_path: event.target.value })} placeholder="auto-generated when empty" />
            </label>
          </div>
          <div className="actions">
            <button className="secondary-button" type="button" onClick={() => void createMachineManifest()} disabled={busyAction === 'manifest' || !manifestStorageReady}>
              {busyAction === 'manifest' ? <Loader2 className="spin" size={16} /> : <UploadCloud size={16} />}
              <span>Create Manifest</span>
            </button>
            <button className="primary-button" type="button" onClick={() => void createManifestAndTrigger()} disabled={!workflowMutationsEnabled || busyAction === 'manifest' || busyAction === 'trigger' || !manifestStorageReady || !argoReady || legacyOneMachineLocked} title={!workflowMutationsEnabled ? 'Workflow operations are disabled in monitor-only runtime' : legacyOneMachineLocked ? 'This machine already has an active backfill' : undefined}>
              {busyAction === 'trigger' ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
              <span>Run Backfill for This Machine</span>
            </button>
          </div>
        </div>

        <div className="panel multi-manifest-panel legacy-backfill-panel">
          <div className="panel-header">
            <div>
              <h2>Backfill Selected Machines</h2>
              <p>Paste exactly the machine IDs you want to run—for example, two machines. Nothing else is included.</p>
            </div>
          </div>
          <div className="manifest-location">
            <span>Destination</span>
            <strong className="mono">aifleetmlopsfstbackfill / fst-backfill</strong>
          </div>
          <div className="form-grid">
            <label className="wide">
              Machine IDs
              <textarea value={multiManifest.machine_ids} onChange={(event) => setMultiManifest({ ...multiManifest, machine_ids: event.target.value })} rows={6} placeholder="One machine ID per line, or comma-separated" />
            </label>
            <DateRangeFields
              since={multiManifest.since}
              until={multiManifest.until}
              onSinceChange={(since) => setMultiManifest({ ...multiManifest, since })}
              onUntilChange={(until) => setMultiManifest({ ...multiManifest, until })}
            />
            <label className="wide">
              Manifest blob path
              <input value={multiManifest.manifest_path} onChange={(event) => setMultiManifest({ ...multiManifest, manifest_path: event.target.value })} placeholder="Auto-generated when empty" />
            </label>
            <label>
              Parallel machine lanes
              <input type="number" min={1} value={trigger.max_parallel_steps} onChange={(event) => setTrigger({ ...trigger, max_parallel_steps: Math.max(1, Number(event.target.value)) })} />
              <small>Runs different machines concurrently; months for each machine remain sequential.</small>
            </label>
          </div>
          {exceedsRecommendedConcurrency ? (
            <section className="inline-notice concurrency-warning" role="alert">
              <AlertTriangle size={16} />
              <span>{trigger.max_parallel_steps} parallel lanes exceeds the recommended cap of {RECOMMENDED_MACHINE_CONCURRENCY}. Reduce it unless capacity and workload have been validated.</span>
            </section>
          ) : (
            <p className="concurrency-guidance">Recommended cap: {RECOMMENDED_MACHINE_CONCURRENCY} parallel machine lanes.</p>
          )}
          <div className="actions">
          <button className="secondary-button" type="button" onClick={() => void createMultiMachineManifest()} disabled={busyAction === 'multi-manifest' || !manifestStorageReady}>
            {busyAction === 'multi-manifest' ? <Loader2 className="spin" size={16} /> : <UploadCloud size={16} />}
            <span>Create Multi-Machine Manifest</span>
          </button>
          <button className="primary-button" type="button" onClick={() => void createMultiManifestAndTrigger()} disabled={!workflowMutationsEnabled || busyAction !== null || !manifestStorageReady || !argoReady || legacySelectedMachinesLocked} title={!workflowMutationsEnabled ? 'Workflow operations are disabled in monitor-only runtime' : legacySelectedMachinesLocked ? 'A selected machine already has an active backfill' : undefined}>
            {busyAction?.startsWith('scoped backfill') ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
            <span>Run Backfill for Selected Machines</span>
          </button>
          </div>
        </div>

        <div className="panel workflow-source-panel advanced-panel admin-view-panel advanced-view-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">Advanced setup</p>
              <h2>Workflow Source</h2>
              <p>Use local code or fetch a flow from GitHub before running Metaflow CLI.</p>
            </div>
          </div>
          <div className="form-grid">
            <label>
              Source
              <select value={source.source_type} onChange={(event) => setSource({ ...source, source_type: event.target.value as SourceType })}>
                <option value="local">Local file</option>
                <option value="github">GitHub ref</option>
              </select>
            </label>
            {source.source_type === 'local' ? (
              <label className="wide">
                Flow file
                <input value={source.local_flow_path} onChange={(event) => setSource({ ...source, local_flow_path: event.target.value })} />
              </label>
            ) : (
              <>
                <label className="wide">
                  GitHub repo
                  <input value={source.github_repo_url} onChange={(event) => setSource({ ...source, github_repo_url: event.target.value })} placeholder="https://github.com/augurysys/metaflow-bx.git" />
                </label>
                <label>
                  Ref
                  <input value={source.github_ref} onChange={(event) => setSource({ ...source, github_ref: event.target.value })} />
                </label>
                <label>
                  Flow path
                  <input value={source.github_flow_path} onChange={(event) => setSource({ ...source, github_flow_path: event.target.value })} />
                </label>
              </>
            )}
          </div>
          <button className="secondary-button" type="button" onClick={() => void submitAdmin('/api/admin/workflows/create', { source }, 'create')} disabled={!workflowMutationsEnabled || busyAction === 'create' || !argoReady}>
            {busyAction === 'create' ? <Loader2 className="spin" size={16} /> : <FileCode2 size={16} />}
            <span>Create Workflow</span>
          </button>
        </div>

        <div className="panel run-backfill-panel all-inventory-panel legacy-backfill-panel">
          <div className="panel-header">
            <div>
              <h2>Backfill Every Inventory Machine</h2>
              <p>Bulk operation only: this creates and runs a manifest for every machine currently in the inventory.</p>
            </div>
          </div>
          <div className="form-grid">
            <label>
              Environment
              <select value={trigger.environment} onChange={(event) => setTrigger({ ...trigger, environment: event.target.value as EnvironmentTarget })}>
                <option value="dev">Dev/Test</option>
                <option value="prod">Production</option>
              </select>
            </label>
            <label>
              Namespace
              <input
                value={trigger.environment === 'prod' ? spec?.prod_namespace ?? PROD_CONTAINER : trigger.namespace}
                disabled={trigger.environment === 'prod'}
                onChange={(event) => setTrigger({ ...trigger, namespace: event.target.value })}
              />
            </label>
            <small className="wide">This submits the same ULRPM parent orchestrator as the main runner. Production fixes the child namespace to feature-store-container; the child uses is_prod to skip dev seeding.</small>
          </div>
          <div className="all-machines-backfill">
            <div>
              <h3>All inventory machines</h3>
              <p>This does not use the selected-machine form above. It targets the entire current inventory.</p>
            </div>
            <div className="form-grid">
              <DateRangeFields
                since={allMachines.since}
                until={allMachines.until}
                onSinceChange={(since) => setAllMachines({ ...allMachines, since })}
                onUntilChange={(until) => setAllMachines({ ...allMachines, until })}
              />
              <label>
                Parallel machine lanes
                <input type="number" min={1} value={trigger.max_parallel_steps} onChange={(event) => setTrigger({ ...trigger, max_parallel_steps: Math.max(1, Number(event.target.value)) })} />
                <small>Runs different machines concurrently; months for each machine remain sequential.</small>
              </label>
              <label className="wide">
                Manifest blob path
                <input value={allMachines.manifest_path} onChange={(event) => setAllMachines({ ...allMachines, manifest_path: event.target.value })} placeholder="Auto-generated unique path when empty" />
              </label>
            </div>
            {exceedsRecommendedConcurrency ? (
              <section className="inline-notice concurrency-warning" role="alert">
                <AlertTriangle size={16} />
                <span>{trigger.max_parallel_steps} parallel lanes exceeds the recommended cap of {RECOMMENDED_MACHINE_CONCURRENCY}. Reduce it unless capacity and workload have been validated.</span>
              </section>
            ) : (
              <p className="concurrency-guidance">Recommended cap: {RECOMMENDED_MACHINE_CONCURRENCY} parallel machine lanes.</p>
            )}
            <button className="secondary-button" type="button" onClick={() => void createAllMachinesManifestAndTrigger()} disabled={!workflowMutationsEnabled || busyAction === 'all-machines-trigger' || !manifestStorageReady || !argoReady || inventoryHasActiveBackfill} title={!workflowMutationsEnabled ? 'Workflow operations are disabled in monitor-only runtime' : inventoryHasActiveBackfill ? 'An inventory machine already has an active backfill' : undefined}>
              {busyAction === 'all-machines-trigger' ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
              <span>Run Backfill for Every Inventory Machine</span>
            </button>
          </div>
        </div>

        <div className="panel full-span fullrlbl-panel advanced-panel admin-view-panel advanced-view-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">Advanced validation</p>
              <h2>FullRlbl Dev Testing</h2>
              <p>Test Severity relabeling against dev FST without touching production. Per-machine OOM monitoring.</p>
            </div>
            {(spec as any)?.fullrlbl_test?.outerbounds_url ? (
              <a className="export-link" href={(spec as any).fullrlbl_test.outerbounds_url} target="_blank" rel="noreferrer">
                <ExternalLink size={16} />
                <span>Runs</span>
              </a>
            ) : null}
          </div>
          <div className="form-grid">
            <label className="wide">
              Machine IDs
              <textarea
                value={fullrlblForm.machine_ids}
                onChange={(event) => setFullrlblForm({ ...fullrlblForm, machine_ids: event.target.value })}
                rows={4}
                placeholder={`Leave empty to use all ${fullrlblMachines.length} inventory machines, or paste IDs (one per line / comma-separated)`}
              />
            </label>
            <label>
              FST Namespace
              <input value={fullrlblForm.fst_namespace} onChange={(event) => setFullrlblForm({ ...fullrlblForm, fst_namespace: event.target.value })} />
            </label>
            <label>
              LST Namespace
              <input value={fullrlblForm.lst_namespace} onChange={(event) => setFullrlblForm({ ...fullrlblForm, lst_namespace: event.target.value })} />
            </label>
            <label>
              Pipeline Name
              <input value={fullrlblForm.pipeline_name} onChange={(event) => setFullrlblForm({ ...fullrlblForm, pipeline_name: event.target.value })} placeholder="Auto-generated when empty" />
            </label>
            <label>
              Test Mode
              <select value={fullrlblForm.test_mode} onChange={(event) => setFullrlblForm({ ...fullrlblForm, test_mode: event.target.value })}>
                <option value="fetch_only">Fetch Only (memory test)</option>
                <option value="full">Full (fetch + inference + persist)</option>
              </select>
            </label>
            <label>
              Feature Fetch Mode
              <select value={fullrlblForm.feature_fetch_mode} onChange={(event) => setFullrlblForm({ ...fullrlblForm, feature_fetch_mode: event.target.value })}>
                <option value="legacy">Legacy (original path)</option>
                <option value="streaming">Streaming (patched)</option>
                <option value="auto">Auto</option>
              </select>
            </label>
            <label>
              Memory (MB)
              <input type="number" min={4096} step={1024} value={fullrlblForm.memory_mb} onChange={(event) => setFullrlblForm({ ...fullrlblForm, memory_mb: Number(event.target.value) })} />
            </label>
            <label className="wide">
              Manifest Path
              <input value={fullrlblForm.manifest_path} onChange={(event) => setFullrlblForm({ ...fullrlblForm, manifest_path: event.target.value })} placeholder="Empty = generate FST-bounded synthetic manifest per machine" />
            </label>
            <label>
              Since
              <input type="datetime-local" value={wideRangeDateTimeValue(fullrlblForm.wide_range_since)} onChange={(event) => setFullrlblForm({ ...fullrlblForm, wide_range_since: wideRangeFromDateTimeValue(event.target.value) })} />
            </label>
            <label>
              Until
              <input type="datetime-local" value={wideRangeDateTimeValue(fullrlblForm.wide_range_until)} onChange={(event) => setFullrlblForm({ ...fullrlblForm, wide_range_until: wideRangeFromDateTimeValue(event.target.value) })} />
            </label>
            <label className="checkbox-field">
              <input type="checkbox" checked={fullrlblForm.runtime_patch} onChange={(event) => setFullrlblForm({ ...fullrlblForm, runtime_patch: event.target.checked })} />
              Enable streaming runtime patch
            </label>
            <label className="checkbox-field">
              <input type="checkbox" checked={fullrlblForm.persist_dev_lst} onChange={(event) => setFullrlblForm({ ...fullrlblForm, persist_dev_lst: event.target.checked })} />
              Persist to dev LST
            </label>
            <label className="checkbox-field">
              <input type="checkbox" checked={fullrlblForm.seed_dev_lst} onChange={(event) => setFullrlblForm({ ...fullrlblForm, seed_dev_lst: event.target.checked })} />
              Seed dev LST from production
            </label>
          </div>
          <div className="actions">
            <button className="primary-button" type="button" onClick={() => void triggerFullRlblTest()} disabled={!workflowMutationsEnabled || busyAction === 'fullrlbl-trigger' || !fullrlblReady}>
              {busyAction === 'fullrlbl-trigger' ? <Loader2 className="spin" size={16} /> : <Play size={16} />}
              <span>
                {fullrlblForm.machine_ids.trim()
                  ? `Run FullRlbl Test (${parseMachineIds(fullrlblForm.machine_ids).length} machines)`
                  : `Run FullRlbl Test (all ${fullrlblMachines.length} machines)`}
              </span>
            </button>
          </div>
          {fullrlblMachines.length > 0 ? (
            <details className="machine-inventory-details">
              <summary>{fullrlblMachines.length} machines in inventory</summary>
              <pre className="log-output">{fullrlblMachines.join('\n')}</pre>
            </details>
          ) : null}
        </div>

        <div className="panel full-span running-tasks-panel admin-view-panel operations-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">Live operations</p>
              <h2>Running Tasks</h2>
              <p>All running Argo workflows. Stop a specific workflow.</p>
            </div>
            <div className="actions">
              <button className="icon-button" type="button" onClick={() => void loadWorkflowData(true)} title="Refresh running tasks" disabled={workflowRefreshing}>
                <RefreshCw className={workflowRefreshing ? 'spin' : ''} size={18} />
              </button>
              <a className="icon-link" href={runningUrl} target="_blank" rel="noreferrer" title="Open running workflows in Outerbounds">
                <ExternalLink size={16} />
              </a>
            </div>
          </div>
          <div className="form-grid">
            <label>
              Stop scope
              <select value={stop.environment} onChange={(event) => setStop({ ...stop, environment: event.target.value as EnvironmentTarget })}>
                <option value="dev">Dev/Test</option>
                <option value="prod">Production</option>
              </select>
            </label>
            {stop.environment === 'prod' ? (
              <>
                <label className="checkbox-field">
                  <input type="checkbox" checked={stop.confirm_production} onChange={(event) => setStop({ ...stop, confirm_production: event.target.checked })} />
                  Confirm production stop
                </label>
                <label>
                  Confirmation
                  <input value={stop.confirmation_text} onChange={(event) => setStop({ ...stop, confirmation_text: event.target.value })} placeholder={spec?.stop_prod_confirmation} />
                </label>
              </>
            ) : null}
          </div>
          {runningWorkflows === null ? <LoadingWidget label="Loading running tasks" /> : <>
          {runningWorkflows.error ? <p className="error-text">{runningWorkflows.error}</p> : null}
          {runningWorkflows.argo_available === false ? (
            <div className="argo-unavailable">
              <AlertTriangle size={16} />
              <div>
                <strong>argo CLI not available locally</strong>
                <p>{runningWorkflows.hint ?? 'View and stop running workflows directly in Outerbounds.'}</p>
              </div>
              <a className="primary-button" href={runningUrl} target="_blank" rel="noreferrer">
                <ExternalLink size={16} />
                <span>Open Outerbounds</span>
              </a>
            </div>
          ) : (
            <div className="workflow-list">
              {workflowGroups.map(([flowName, flowWorkflows], index) => (
                <section className={`workflow-flow-group flow-tone-${index % 4}`} key={flowName}>
                  <div className="workflow-flow-header">
                    <div><span className="flow-index">Run lane {String(index + 1).padStart(2, '0')}</span><h3>{flowName}</h3></div>
                    <span>{flowWorkflows.length} running</span>
                  </div>
                  {Object.entries(flowWorkflows.reduce<Record<string, RunningWorkflow[]>>((groups, workflow) => {
                    const machineKeys = workflow.machine_ids?.length ? workflow.machine_ids : ['Machine not linked'];
                    machineKeys.forEach((machineId) => { (groups[machineId] ??= []).push(workflow); });
                    return groups;
                  }, {})).map(([machineId, machineWorkflows]) => (
                    <section className="workflow-machine-group" key={machineId}>
                      <div className="workflow-machine-header"><span>Machine</span><strong className="mono">{machineId}</strong>{machineId !== 'Machine not linked' ? <><CopyMachineIdButton machineId={machineId} /><a className="secondary-button machine-detail-link" href={detailMachineHref(machineId)}><Eye size={15} /><span>Go to machine</span></a></> : null}<span>{machineWorkflows.length} run{machineWorkflows.length === 1 ? '' : 's'}</span></div>
                      {machineWorkflows.map((workflow) => (
                    <article className="workflow-card" key={workflow.workflow_id}>
                      <div>
                        <strong className="mono break">{workflow.workflow_id}</strong>
                        <span>{workflow.status} · started {formatTimestamp(workflow.started_at ?? workflow.created_at)}</span>
                      </div>
                      <div className="actions">
                        <a className="icon-link" href={workflow.outerbounds_url} target="_blank" rel="noreferrer" title="Open in Outerbounds">
                          <ExternalLink size={16} />
                        </a>
                        <button
                          className="icon-link danger"
                          type="button"
                          title="Stop this workflow"
                          onClick={() =>
                            void submitAdmin('/api/admin/workflows/terminate', {
                              params: {
                                ...stop,
                                workflow_id: workflow.workflow_id,
                              },
                            }, `stop-${workflow.workflow_id}`)
                          }
                          disabled={!workflowMutationsEnabled || busyAction === `stop-${workflow.workflow_id}` || !argoReady}
                        >
                          {busyAction === `stop-${workflow.workflow_id}` ? <Loader2 className="spin" size={16} /> : <Square size={16} />}
                        </button>
                      </div>
                    </article>
                      ))}
                    </section>
                  ))}
                </section>
              ))}
              {workflows.length === 0 ? <div className="empty-state">No running workflows found.</div> : null}
            </div>
          )}</>}
        </div>

        <div className="panel full-span logs-panel advanced-panel admin-view-panel observability-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">Observability</p>
              <h2>Logs</h2>
              <p>Fetch task logs via argo CLI (if available) or Metaflow. Format: `run/step/task`.</p>
            </div>
          </div>
          <div className="form-grid">
            <label className="wide">
              Run task path
              <input value={logs.run_task_path} onChange={(event) => setLogs({ ...logs, run_task_path: event.target.value })} placeholder="argo-fstbackfill.../anomaly_features/t-..." />
            </label>
            <label>
              Stream
              <select value={logs.stream} onChange={(event) => setLogs({ ...logs, stream: event.target.value as LogsForm['stream'] })}>
                <option value="stdout">stdout</option>
                <option value="stderr">stderr</option>
              </select>
            </label>
          </div>
          <button className="secondary-button" type="button" onClick={() => void submitAdmin('/api/admin/logs', { source, ...logs }, 'logs')} disabled={!workflowMutationsEnabled || busyAction === 'logs'}>
            {busyAction === 'logs' ? <Loader2 className="spin" size={16} /> : <Terminal size={16} />}
            <span>Fetch Logs</span>
          </button>
        </div>

        <div className="panel full-span reference-panel advanced-panel admin-view-panel observability-panel">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">Reference</p>
              <h2>Reference</h2>
              <p>{spec?.known_no_data_note ?? 'Loading backfill parameters...'}</p>
            </div>
            {spec ? (
              <div className="actions">
                <a className="export-link" href={runningUrl} target="_blank" rel="noreferrer">
                  <ExternalLink size={16} />
                  <span>Running</span>
                </a>
                <a className="export-link" href={pastUrl} target="_blank" rel="noreferrer">
                  <History size={16} />
                  <span>Past</span>
                </a>
              </div>
            ) : null}
          </div>
          {spec === null ? <LoadingWidget label="Loading backfill reference" /> : <div className="param-list">
            {spec.params.map((param) => (
              <div key={param.name}>
                <strong>{param.name}</strong>
                <span>{param.description}</span>
              </div>
            ))}
          </div>}
        </div>
      </section>

      {createdManifest ? (
        <ManifestCreatedDialog
          manifest={createdManifest}
          onClose={() => setCreatedManifest(null)}
          onCopy={() => {
            void navigator.clipboard.writeText(createdManifest.manifest_path);
            onToast('success', 'Manifest blob path copied for the backfill trigger.');
          }}
        />
      ) : null}

      <section className={`panel action-history-panel admin-view-panel observability-panel${showAdvanced ? '' : ' advanced-panel'}`}>
        <div className="panel-header">
          <div>
            <p className="panel-kicker">Audit trail</p>
            <h2>Action History</h2>
            <p>Create, trigger, and log commands executed by the API.</p>
          </div>
          <button className="icon-button" type="button" onClick={() => void loadAdminReference(true)} title="Refresh actions" disabled={referenceRefreshing}>
            <RefreshCw className={referenceRefreshing ? 'spin' : ''} size={18} />
          </button>
        </div>
        <div className="action-list-container">
          <div className="action-list">
          {adminLoading ? <LoadingWidget label="Loading action history" /> : actions.map((action) => (
            <article className="action-card" key={action.id}>
              <div className="action-head">
                <span className={`action-status ${action.status}`}>{action.status}</span>
                <strong>{action.action}</strong>
                <small>{formatTimestamp(action.created_at)}</small>
              </div>
              <code>{action.command.join(' ')}</code>
              <div className="action-links">
                {action.outerbounds_url ? (
                  <a className="export-link" href={action.status === 'running' ? runningUrl : action.outerbounds_url} target="_blank" rel="noreferrer">
                    <ExternalLink size={16} />
                    <span>{action.status === 'running' ? 'Running' : 'Outerbounds'}</span>
                  </a>
                ) : null}
                <a className="export-link" href={pastUrl} target="_blank" rel="noreferrer">
                  <History size={16} />
                  <span>Past Runs</span>
                </a>
              </div>
              {action.error ? <p className="error-text">{action.error}</p> : null}
              {action.stdout || action.stderr ? <LogViewer stdout={action.stdout} stderr={action.stderr} /> : null}
            </article>
          ))}
          {actions.length === 0 ? <div className="empty-state">No admin actions yet.</div> : null}
          </div>
        </div>
      </section>
      </div>
    </div>
    </>
  );
}

function SandboxTab() {
  const [machineId, setMachineId] = useState('683ec59079fecb5a5a240478');
  const [since, setSince] = useState('2026-02-01T00:00:00');
  const [until, setUntil] = useState('2026-03-01T00:00:00');
  const [environment, setEnvironment] = useState<EnvironmentTarget>('dev');
  const [namespace, setNamespace] = useState(DEFAULT_DEV_NAMESPACE);
  const [includeFeatures, setIncludeFeatures] = useState(false);
  const [features, setFeatures] = useState('');
  const [maxParallelSteps, setMaxParallelSteps] = useState(1);
  const [events, setEvents] = useState<SandboxEvent[]>([]);
  const manifestPath = buildSandboxManifestPath(machineId, since, until);
  const command = buildSandboxTriggerCommand({
    environment,
    namespace,
    manifestPath,
    includeFeatures,
    features,
    maxParallelSteps,
  });

  const addEvent = (label: string, eventCommand: string) => {
    setEvents((current) => [
      {
        id: crypto.randomUUID(),
        created_at: new Date().toISOString(),
        label,
        command: eventCommand,
      },
      ...current,
    ]);
  };

  return (
    <>
      <section className="sandbox-grid">
        <div className="panel">
          <div className="panel-header">
            <div>
              <h2>Sandbox Inputs</h2>
              <p>Local-only previews for machine-specific backfill controls.</p>
            </div>
          </div>
          <div className="form-grid">
            <label className="wide">
              Machine ID
              <input value={machineId} onChange={(event) => setMachineId(event.target.value)} />
            </label>
            <DateRangeFields since={since} until={until} onSinceChange={setSince} onUntilChange={setUntil} />
            <label>
              Environment
              <select value={environment} onChange={(event) => setEnvironment(event.target.value as EnvironmentTarget)}>
                <option value="dev">Dev/Test</option>
                <option value="prod">Production</option>
              </select>
            </label>
            <label>
              Namespace
              <input value={environment === 'prod' ? PROD_CONTAINER : namespace} disabled={environment === 'prod'} onChange={(event) => setNamespace(event.target.value)} />
            </label>
            <label className="checkbox-field wide">
              <input type="checkbox" checked={includeFeatures} onChange={(event) => setIncludeFeatures(event.target.checked)} />
              Send feature whitelist parameter
            </label>
            {includeFeatures ? (
              <label className="wide">
                Features
                <textarea value={features} onChange={(event) => setFeatures(event.target.value)} rows={3} placeholder="comma-separated; empty runs all columns" />
              </label>
            ) : null}
            <label>
              Max parallel steps
              <input type="number" min={1} value={maxParallelSteps} onChange={(event) => setMaxParallelSteps(Number(event.target.value))} />
            </label>
          </div>
          <div className="actions">
            <button className="secondary-button" type="button" onClick={() => addEvent('Preview manifest', manifestPath)}>
              <UploadCloud size={16} />
              <span>Preview Manifest</span>
            </button>
            <button className="secondary-button" type="button" onClick={() => addEvent('Preview trigger', command)}>
              <Play size={16} />
              <span>Preview Trigger</span>
            </button>
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <div>
              <h2>Command Preview</h2>
              <p>No API calls, blob writes, or workflow actions run from this tab.</p>
            </div>
          </div>
          <div className="sandbox-preview">
            <span>Manifest path</span>
            <code>{manifestPath}</code>
            <span>Trigger command</span>
            <pre>{command}</pre>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <h2>Sandbox History</h2>
            <p>Local preview events from this browser session.</p>
          </div>
          <button className="icon-button" type="button" onClick={() => setEvents([])} title="Clear sandbox history">
            <RefreshCw size={18} />
          </button>
        </div>
        <div className="action-list">
          {events.map((event) => (
            <article className="action-card" key={event.id}>
              <div className="action-head">
                <span className="action-status queued">sandbox</span>
                <strong>{event.label}</strong>
                <small>{formatTimestamp(event.created_at)}</small>
              </div>
              <pre>{event.command}</pre>
            </article>
          ))}
          {events.length === 0 ? <div className="empty-state">No sandbox events yet.</div> : null}
        </div>
      </section>
    </>
  );
}

function MetricCard({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string | number;
  detail: string;
  tone: 'neutral' | 'success' | 'warning';
}) {
  const numericValue = typeof value === 'number' ? value : Number.parseInt(value, 10);
  return (
    <article className={`metric-card ${tone}`}>
      <span>{label}</span>
      <strong>{Number.isFinite(numericValue) ? <AnimatedNumber value={numericValue} suffix={typeof value === 'string' && value.endsWith('%') ? '%' : ''} /> : value}</strong>
      <small>{detail}</small>
    </article>
  );
}

function AnimatedNumber({ value, suffix = '', duration = 600 }: { value: number; suffix?: string; duration?: number }) {
  const [display, setDisplay] = useState(0);
  const frame = useRef<number | null>(null);
  useEffect(() => {
    const start = display;
    const startedAt = performance.now();
    const step = (timestamp: number) => {
      const progress = Math.min((timestamp - startedAt) / duration, 1);
      setDisplay(Math.round(start + (value - start) * (1 - Math.pow(1 - progress, 3))));
      if (progress < 1) frame.current = requestAnimationFrame(step);
    };
    frame.current = requestAnimationFrame(step);
    return () => { if (frame.current) cancelAnimationFrame(frame.current); };
  }, [value, duration]);
  return <>{formatNumber(display)}{suffix}</>;
}



function RunnerMonthCards({ selected, onChange, availability, machineCount, skippedCount }: {
  selected: number[];
  onChange: (indices: number[]) => void;
  availability: Record<number, number>;
  machineCount: number;
  skippedCount: number;
}) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const interval = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(interval);
  }, []);
  const months = orchestratorMonthsThroughToday(now).map(({ index, year, month }) => ({
    index,
    label: new Date(Date.UTC(year, month - 1, 1)).toLocaleString('en', {
      month: 'short',
      year: 'numeric',
      timeZone: 'UTC',
    }),
  }));
  return <section className="wide runner-month-cards"><div className="runner-month-cards-head"><div><strong>Pick individual calendar months</strong><span>{selected.length ? `${selected.length} selected` : 'No months selected'} · month labels show online/backfillable machines</span>{skippedCount ? <span>{skippedCount} non-online or unavailable machine-months skipped.</span> : null}</div><button className="secondary-button" type="button" onClick={() => onChange([])} disabled={selected.length === 0}>Clear selection</button></div><div>{months.map((month) => { const availableCount = availability[month.index] ?? 0; return <button key={month.index} className={selected.includes(month.index) ? 'active' : ''} type="button" aria-pressed={selected.includes(month.index)} aria-label={`${month.label}, available for ${availableCount} of ${machineCount} selected machines`} title={`${availableCount} of ${machineCount} selected machines have this month online and backfillable`} disabled={availableCount === 0} onClick={() => onChange(selected.includes(month.index) ? selected.filter((index) => index !== month.index) : [...selected, month.index])}>{month.label} · {availableCount}/{machineCount}</button>; })}</div></section>;
}

function DateRangeFields({
  since,
  until,
  onSinceChange,
  onUntilChange,
}: {
  since: string;
  until: string;
  onSinceChange: (value: string) => void;
  onUntilChange: (value: string) => void;
}) {
  const [entryMode, setEntryMode] = useState<'calendar' | 'text'>('calendar');
  const isCalendar = entryMode === 'calendar';
  const update = (value: string, setter: (next: string) => void) => {
    setter(isCalendar ? value : value.trim());
  };

  return (
    <>
      <div className="date-entry-mode wide" role="group" aria-label="Date entry mode">
        <span>Date entry</span>
        <button className={isCalendar ? 'active' : ''} type="button" onClick={() => setEntryMode('calendar')}>Calendar</button>
        <button className={entryMode === 'text' ? 'active' : ''} type="button" onClick={() => setEntryMode('text')}>Text</button>
      </div>
      <label>
        Since
        <input
          type={isCalendar ? 'date' : 'text'}
          max={isCalendar ? utcToday() : undefined}
          value={isCalendar ? dateOnlyValue(since) : since}
          onChange={(event) => update(event.target.value, onSinceChange)}
          placeholder="YYYY-MM-DD; midnight is assumed"
        />
      </label>
      <label>
        Until
        <input
          type={isCalendar ? 'date' : 'text'}
          max={isCalendar ? utcToday() : undefined}
          value={isCalendar ? dateOnlyValue(until) : until}
          onChange={(event) => update(event.target.value, onUntilChange)}
          placeholder="YYYY-MM-DD; midnight is assumed"
        />
      </label>
    </>
  );
}




function MachineRuns({ machineId, mode = 'history' }: { machineId: string; mode?: 'history' | 'running' }) {
  const [runs, setRuns] = useState<AdminAction[] | null>(null);
  const [confirmedRunningWorkflowIds, setConfirmedRunningWorkflowIds] = useState<Set<string>>(new Set());
  const [liveWorkflowStatuses, setLiveWorkflowStatuses] = useState<Record<string, string>>({});
  const isExpanded = true;
  const [refreshing, setRefreshing] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  useEffect(() => {
    getCachedJson<{ actions: AdminAction[]; confirmed_running_workflow_ids?: string[]; live_workflow_statuses?: Record<string, string> }>(`machine-runs:${machineId}`, `${API_BASE}/api/admin/actions?machine_id=${encodeURIComponent(machineId)}`, 15_000, true)
      .then((payload) => {
        setRuns(payload.actions);
        setConfirmedRunningWorkflowIds(new Set(payload.confirmed_running_workflow_ids ?? []));
        setLiveWorkflowStatuses(payload.live_workflow_statuses ?? {});
      })
      .catch(() => { setRuns([]); setConfirmedRunningWorkflowIds(new Set()); setLiveWorkflowStatuses({}); });
  }, [machineId, refreshVersion]);
  useEffect(() => {
    if (mode !== 'running') return;
    const interval = window.setInterval(() => setRefreshVersion((version) => version + 1), 15_000);
    return () => window.clearInterval(interval);
  }, [mode]);
  const refresh = () => { setRefreshing(true); setRefreshVersion((version) => version + 1); window.setTimeout(() => setRefreshing(false), 500); };
  const workflowRuns = (runs ?? []).filter((run) => run.action === 'trigger');
  // The live set is authoritative for the Running tab. A submitted workflow
  // that is no longer in it belongs in activity history even when the Argo
  // status service cannot provide its final Succeeded/Failed phase.
  const inactiveUnreconciledRuns = workflowRuns.filter((run) => Boolean(run.workflow_id) && !confirmedRunningWorkflowIds.has(run.workflow_id!) && ['queued', 'running'].includes(run.status));
  const taskPhase = (run: AdminAction, section: string) => {
    if (section === 'finished' && ['queued', 'running'].includes(run.status) && !confirmedRunningWorkflowIds.has(run.workflow_id ?? '')) {
      return { key: 'finished', label: 'Finished · result pending' };
    }
    const phase = (liveWorkflowStatuses[run.workflow_id ?? ''] ?? run.status).toLowerCase();
    if (/(queued|pending|waiting)/.test(phase)) return { key: 'queued', label: 'Queued' };
    if (phase.includes('running')) return { key: 'running', label: 'Running' };
    if (/(failed|error)/.test(phase)) return { key: 'failed', label: 'Failed' };
    if (/(succeeded|completed)/.test(phase)) return { key: 'finished', label: 'Finished' };
    return { key: 'unknown', label: phase || 'Status unavailable' };
  };
  const activeRuns = workflowRuns.filter((run) => Boolean(run.workflow_id) && confirmedRunningWorkflowIds.has(run.workflow_id!));
  const queuedCount = activeRuns.filter((run) => taskPhase(run, 'running').key === 'queued').length;
  const runningCount = activeRuns.filter((run) => taskPhase(run, 'running').key === 'running').length;
  const sections: Array<{ title: string; className: string; runs: AdminAction[] }> = [
    { title: 'Running', className: 'running', runs: workflowRuns.filter((run) => Boolean(run.workflow_id) && confirmedRunningWorkflowIds.has(run.workflow_id!)) },
    { title: 'Finished', className: 'finished', runs: [...workflowRuns.filter((run) => run.status === 'succeeded'), ...inactiveUnreconciledRuns] },
    { title: 'Failed', className: 'failed', runs: workflowRuns.filter((run) => run.status === 'failed') },
  ].filter((section) => mode === 'running' ? section.className === 'running' : section.className !== 'running');
  return <section className="panel machine-runs-panel">
    <div className="panel-header"><div><p className="eyebrow">{mode === 'running' ? 'Live operations' : 'Run history'}</p><h2>{mode === 'running' ? 'Running tasks' : 'Runs for this machine'}</h2><p>Only dashboard runs submitted for {machineId} appear here.</p></div><div className="actions">{mode === 'running' ? <div className="task-phase-summary" aria-label="Live task counts"><span className="task-phase-badge running">{runningCount} running</span><span className="task-phase-badge queued">{queuedCount} queued</span></div> : null}<button className="icon-button" type="button" onClick={refresh} disabled={refreshing} title="Refresh machine runs"><RefreshCw className={refreshing ? 'spin' : ''} size={16} /></button></div></div>
    {isExpanded ? (runs === null ? <LoadingWidget label="Loading machine runs" /> : workflowRuns.length === 0 ? <EmptyState message="No dashboard workflow runs have been recorded for this machine yet." /> : <div className={`machine-run-sections${mode === 'running' ? ' running-only' : ''}`}>{sections.map((section) => <section className={`machine-run-section ${section.className}`} key={section.className}><div className="machine-run-section-head"><h3>{section.title}</h3><span>{section.runs.length}</span></div>{section.runs.length === 0 ? <p className="machine-run-empty">No {section.title.toLowerCase()} runs.</p> : <div className="machine-run-list">{Object.entries(section.runs.reduce<Record<string, AdminAction[]>>((groups, run) => { const name = run.flow_name ?? 'Dashboard operation'; (groups[name] ??= []).push(run); return groups; }, {})).map(([flowName, flowRuns]) => <section className="machine-flow-group" key={flowName}><h4>{flowName}</h4>{flowRuns.map((run) => { const phase = taskPhase(run, section.className); return <article className={`machine-run-row task-${phase.key}`} key={run.id}><div><strong>{run.flow_name ?? flowName}</strong><span className="mono">{run.workflow_id ?? 'Submission pending'}</span><span className={`task-phase-badge ${phase.key}`}>{phase.label}</span><span className="task-run-time">Started {formatTimestamp(run.created_at)}</span></div>{run.outerbounds_url ? <a className="icon-link" href={run.outerbounds_url} target="_blank" rel="noreferrer" title="Open this workflow in Outerbounds"><ExternalLink size={16} /></a> : null}</article>; })}</section>)}</div>}</section>)}</div>) : null}
  </section>;
}

function MachineTaskLogs({ machineId }: { machineId: string }) {
  const [runs, setRuns] = useState<AdminAction[] | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  useEffect(() => { getCachedJson<{ actions: AdminAction[] }>(`machine-logs:${machineId}`, `${API_BASE}/api/admin/actions?machine_id=${encodeURIComponent(machineId)}`, 15_000, true).then((payload) => setRuns(payload.actions)).catch(() => setRuns([])); }, [machineId, refreshVersion]);
  const logRuns = (runs ?? []).filter((run) => run.stdout || run.stderr || run.error);
  return <section className="panel machine-observability-panel"><div className="panel-header"><div><p className="eyebrow">Observability</p><h2>Task logs</h2><p>Command output and errors for runs submitted for this machine.</p></div><button className="icon-button" type="button" onClick={() => setRefreshVersion((version) => version + 1)} title="Refresh task logs"><RefreshCw size={16} /></button></div>{runs === null ? <LoadingWidget label="Loading task logs" /> : logRuns.length === 0 ? <EmptyState message="No task output has been recorded for this machine yet." /> : <div className="machine-task-log-list">{logRuns.map((run) => <section className="machine-task-log" key={run.id}><div className="machine-task-log-head"><div><strong>{run.flow_name ?? 'Dashboard operation'}</strong><span className="mono">{run.workflow_id ?? 'Submission pending'}</span></div>{run.outerbounds_url ? <a className="icon-link" href={run.outerbounds_url} target="_blank" rel="noreferrer" title="Open this workflow in Outerbounds"><ExternalLink size={16} /></a> : null}</div>{run.error ? <p className="error-text">{run.error}</p> : null}<LogViewer stdout={run.stdout} stderr={run.stderr} /></section>)}</div>}</section>;
}

function MachineDetails({
  machine,
  selectedMonthIndices = [],
  onToggleBackfillMonth,
  activeMonths = {},
  backfillDisabled = false,
  runningMonthIndex,
  compact = false,
}: {
  machine: MachineStatus;
  selectedMonthIndices?: number[];
  onToggleBackfillMonth?: (month: MonthStatus) => void;
  activeMonths?: Record<number, ActiveMonth>;
  backfillDisabled?: boolean;
  runningMonthIndex?: number;
  compact?: boolean;
}) {
  const targetMonths = machine.months;
  const monthsByYear = targetMonths.reduce<Record<number, Array<{ month: MonthStatus; index: number }>>>((groups, month, index) => {
    (groups[month.partition.year] ??= []).push({ month, index });
    return groups;
  }, {});
  const [selectedIdx, setSelectedIdx] = useState<number>(() => {
    const actionIdx = targetMonths.findIndex((m) => m.status === 'needs_backfill');
    if (actionIdx >= 0) return actionIdx;
    const nonBackfilled = targetMonths.findIndex((m) => m.status !== 'backfilled');
    return nonBackfilled >= 0 ? nonBackfilled : 0;
  });
  useEffect(() => {
    if (targetMonths[selectedIdx]) return;
    const actionIdx = targetMonths.findIndex((month) => month.status === 'needs_backfill');
    const nonBackfilled = targetMonths.findIndex((month) => month.status !== 'backfilled');
    setSelectedIdx(actionIdx >= 0 ? actionIdx : nonBackfilled >= 0 ? nonBackfilled : 0);
  }, [machine.machine_id, targetMonths, selectedIdx]);
  const selectedMonth = targetMonths[selectedIdx]
    ?? targetMonths.find((month) => month.status === 'needs_backfill')
    ?? targetMonths[0];
  return (
    <div className={`details-grid${compact ? ' quick-details-grid' : ''}`}>
      <div className="month-coverage">
        <div className="heatmap">
          {Object.entries(monthsByYear).map(([year, months]) => (
            <section className="month-year-group" key={year} aria-label={`${year} monthly partitions`}>
              <span className="month-year-label">{year}</span>
              <div className="month-year-cards">
                {months.map(({ month, index }) => {
                  const fixedMonthIndex = orchestratorMonthIndex(month.partition);
                  const isSelectedForBackfill = selectedMonthIndices.includes(fixedMonthIndex);
                  const active = activeMonths[fixedMonthIndex];
                  const canBackfill = isMonthBackfillable(month) && !active && !backfillDisabled;
                  const activityStatus = activityForMonth(month);
                  const nonBackfillableReason = activityStatus === 'offline'
                    ? 'Offline — no FST data; backfill disabled to protect the pipeline'
                    : activityStatus === 'not_installed'
                      ? 'Not installed — backfill disabled to protect the pipeline'
                      : activityStatus === 'unknown'
                        ? 'Unknown activity — backfill disabled to protect the pipeline'
                        : month.reason;
                  return <button
                    aria-label={`${month.partition.label}: ${labelForActivity(activityStatus)}; ${active ? `backfill ${active.state}` : isSelectedForBackfill ? 'selected for backfill' : canBackfill ? 'available for backfill' : nonBackfillableReason}`}
                    aria-pressed={onToggleBackfillMonth ? isSelectedForBackfill : undefined}
                    className={`month-cell ${month.status} activity-${activityStatus}${!isMonthBackfillable(month) ? ' non-backfillable' : ''}${active ? ` active-month ${active.state}` : ''}${fixedMonthIndex === runningMonthIndex ? ' running-month' : ''}${index === selectedIdx ? ' selected' : ''}${isSelectedForBackfill ? ' backfill-selected' : ''}`}
                    key={`${month.partition.label}-${month.partition.index}`}
                    title={active ? `${month.partition.label}: ${active.state}${active.parent_workflow_id ? ` · ${active.parent_workflow_id}` : ''}` : isMonthBackfillable(month) ? (onToggleBackfillMonth ? `${month.partition.label}: click to ${isSelectedForBackfill ? 'remove from' : 'add to'} backfill selection` : `${month.partition.label}: ${month.reason}`) : `${month.partition.label}: ${nonBackfillableReason}`}
                    type="button"
                    disabled={!isMonthBackfillable(month) || (Boolean(onToggleBackfillMonth) && !canBackfill)}
                    onClick={() => {
                      setSelectedIdx(index);
                    if (onToggleBackfillMonth && canBackfill) onToggleBackfillMonth(month);
                    }}
                  >
                    <span>{new Date(month.partition.year, month.partition.month - 1).toLocaleString('en', { month: 'short' })}</span>
                    {active ? <small>{active.state.replace('_', ' ')}</small> : null}
                    <small className="month-activity-label">{labelForActivity(activityStatus)}</small>
                  </button>;
                })}
              </div>
            </section>
          ))}
        </div>
        <div className="legend" aria-label="Partition status legend">
          <span className="legend-item activity-online"><i className="legend-box" />Online: FST data exists</span>
          <span className="legend-item activity-offline"><i className="legend-box" />Offline: no FST data</span>
          <span className="legend-item activity-not_installed"><i className="legend-box" />Not installed</span>
          <span className="legend-item backfilled"><i className="legend-box backfilled" />Backfilled</span>
          <span className="legend-item needs_backfill"><i className="legend-box needs_backfill" />Needs backfill</span>
          <span className="legend-item scan_error"><i className="legend-box scan_error" />Scan error</span>
          {onToggleBackfillMonth ? <span className="legend-item backfill-selection"><i className="legend-box backfill-selection" />Selected for backfill</span> : null}
        </div>
      </div>

      {!compact ? <div className="month-summary-table-container">
        <h3>Month-by-Month Overview</h3>
        <table className="month-summary-table">
          <thead>
            <tr>
              <th>Month</th>
              <th>Data state</th>
              <th>Backfill state</th>
              <th>Rows</th>
              <th>Total Cols</th>
              <th>Schema Missing</th>
              <th>v2 Present</th>
              <th>v2 Gaps</th>
            </tr>
          </thead>
          <tbody>
            {targetMonths.map((month, idx) => {
              const v2Present = Object.values(month.feature_non_null_counts ?? {}).filter((c) => c > 0).length;
              const v2Missing = (month.missing_features?.length ?? 0) + (month.zero_count_features?.length ?? 0) + (month.partial_features?.length ?? 0);
              return (
                <tr
                  key={month.partition.label}
                  className={`${month.status}${backfillDisabled ? ' backfill-locked-row' : ''}${!isMonthBackfillable(month) ? ' non-backfillable-row' : ''}${idx === selectedIdx ? ' selected-row' : ''}${selectedMonthIndices.includes(orchestratorMonthIndex(month.partition)) && isMonthBackfillable(month) ? ' backfill-selected-row' : ''}`}
                  onClick={() => {
                    if (backfillDisabled) return;
                    setSelectedIdx(idx);
                    if (onToggleBackfillMonth && isMonthBackfillable(month) && !backfillDisabled) onToggleBackfillMonth(month);
                  }}
                >
                  <td className="mono">{month.partition.label}</td>
                  <td><span className={`activity-badge ${activityForMonth(month)}`}>{labelForActivity(activityForMonth(month))}</span></td>
                  <td><span className={`status-badge ${month.status}`}>{month.status.replace('_', ' ')}</span></td>
                  <td>{month.row_count == null ? '—' : formatNumber(month.row_count)}</td>
                  <td>{month.total_columns == null ? '—' : month.total_columns}</td>
                  <td>{(month.missing_schema_columns?.length ?? 0) > 0 ? <span className="text-warn">{month.missing_schema_columns.length}</span> : month.row_count == null ? '—' : <span className="text-ok">0</span>}</td>
                  <td>{month.row_count == null ? '—' : `${v2Present}/8`}</td>
                  <td>{v2Missing > 0 ? <span className="text-warn">{v2Missing}</span> : month.row_count == null ? '—' : <span className="text-ok">0</span>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div> : null}

      <div className="detail-card month-detail-card">
        <div className="month-detail-heading">
          <div>
            <p className="month-detail-kicker">Month details</p>
            <h3>{selectedMonth?.partition.label ?? 'Choose a month'}</h3>
          </div>
          {selectedMonth ? <StatusPill status={selectedMonth.status} /> : null}
        </div>
        <p className="month-detail-reason">{selectedMonth?.reason ?? 'Select a month in the timeline to view its partition details.'}</p>
        <dl className="month-detail-list">
          <dt>Recommended action</dt>
          <dd>{selectedMonth?.recommended_action ?? machine.recommended_action}</dd>
          <dt>Lifecycle / data state</dt>
          <dd>{selectedMonth ? labelForActivity(activityForMonth(selectedMonth)) : 'Unknown'}</dd>
          <dt>Installed at</dt>
          <dd>{machine.installation_at ? formatTimestamp(machine.installation_at) : 'Unavailable from Mongo'}</dd>
          <dt>Production rows</dt>
          <dd>{selectedMonth?.row_count == null ? 'Unknown' : formatNumber(selectedMonth.row_count)}</dd>
          <dt>Total columns in parquet</dt>
          <dd>{selectedMonth?.total_columns == null ? 'Unknown' : selectedMonth.total_columns}</dd>
          <dt>Expected full-schema columns</dt>
          <dd>{selectedMonth?.expected_total_columns == null ? 'Not applicable' : selectedMonth.expected_total_columns}</dd>
          <dt>Silver rows</dt>
          <dd>{selectedMonth?.silver_row_count == null ? 'Not checked' : formatNumber(selectedMonth.silver_row_count)}</dd>
          <dt>Last modified</dt>
          <dd>{formatTimestamp(selectedMonth?.last_modified)}</dd>
          {!compact ? <><dt>Blob path</dt>
          <dd className="mono break">{selectedMonth?.blob_path ?? 'Unknown'}</dd></> : null}
        </dl>
      </div>

      {!compact && (selectedMonth?.missing_schema_columns?.length ?? 0) > 0 ? <div className="detail-card">
        <h3>Missing Full-Schema Columns</h3>
        <p className="muted">Compared with the versioned canonical Feature Store schema.</p>
        <div className="feature-list">
          {selectedMonth?.missing_schema_columns.map((column) => (
            <div className="feature-row missing" key={column}>
              <span className="mono">{column}</span>
              <strong>MISSING</strong>
            </div>
          ))}
        </div>
      </div> : null}

      {!compact ? <div className="detail-card">
        <h3>Target Features (v2 Ultrasonic)</h3>
        <div className="feature-list">
          {Object.entries(selectedMonth?.feature_non_null_counts ?? {}).map(([feature, count]) => {
            const isMissing = selectedMonth?.missing_features?.includes(feature);
            const isZero = selectedMonth?.zero_count_features?.includes(feature);
            const coverageGap = selectedMonth?.feature_coverage_gaps?.[feature];
            const isPartial = selectedMonth?.partial_features?.includes(feature);
            return (
              <div className={`feature-row${isMissing ? ' missing' : isZero ? ' zero' : isPartial ? ' missing' : ' present'}`} key={feature}>
                <span className="mono">{feature}</span>
                <strong title={coverageGap ? `${coverageGap.first_missing_at ?? 'unknown'} — ${coverageGap.last_missing_at ?? 'unknown'}` : undefined}>
                  {isMissing ? 'MISSING' : isPartial ? `${formatNumber(coverageGap?.missing_rows ?? 0)} MISSING` : formatNumber(count)}
                </strong>
              </div>
            );
          })}
          {Object.keys(selectedMonth?.feature_non_null_counts ?? {}).length === 0 ? (
            <p className="muted">No feature counts available for this month.</p>
          ) : null}
        </div>
      </div> : null}

    </div>
  );
}

function sourceLabel(snapshot?: DashboardSnapshot) {
  if (!snapshot) {
    return 'No source loaded';
  }
  return `${snapshot.source_account}/${snapshot.source_container}`;
}

function dateOnlyValue(value: string) {
  return /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : '';
}

function wideRangeDateTimeValue(value: string) {
  const match = value.match(/^(\d{4})[/-](\d{2})[/-](\d{2})[T/ ]?(\d{2})?/);
  return match ? `${match[1]}-${match[2]}-${match[3]}T${match[4] ?? '00'}:00` : '';
}

function wideRangeFromDateTimeValue(value: string) {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2})/);
  return match ? `${match[1]}/${match[2]}/${match[3]}/${match[4]}` : value;
}

function getSavedBlobSource() {
  try {
    const parsed = JSON.parse(localStorage.getItem(BLOB_SOURCE_STORAGE_KEY) ?? '{}') as {
      account?: unknown;
      container?: unknown;
    };
    if (typeof parsed.account === 'string' && parsed.account.trim() && typeof parsed.container === 'string' && parsed.container.trim()) {
      return { account: parsed.account, container: parsed.container };
    }
  } catch {
    // Fall back to production defaults if a prior browser value is malformed.
  }
  return { account: PROD_ACCOUNT, container: PROD_CONTAINER };
}

function labelForStatus(status: BackfillStatus | 'all') {
  return status
    .split('_')
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(' ');
}

function triggerPhaseLabel(phase?: string) {
  const labels: Record<string, string> = {
    queued: 'queued',
    validating: 'validating machine data',
    preparing_manifests: 'preparing manifests',
    submitting: 'submitting to Outerbounds',
    accepted: 'accepted by Outerbounds',
    failed: 'failed',
  };
  return phase ? labels[phase] ?? phase.replaceAll('_', ' ') : 'preparing';
}

function isTriggerPreparationActive(action: AdminAction | null | undefined) {
  return Boolean(action && ['queued', 'validating', 'preparing_manifests', 'submitting'].includes(action.phase ?? ''));
}

function activityForMonth(month: MonthStatus) {
  if (month.activity_status) return month.activity_status;
  if ((month.row_count ?? 0) > 0) return 'online' as const;
  if (month.status === 'no_source_data') return 'offline' as const;
  return 'unknown' as const;
}

function labelForActivity(status: ReturnType<typeof activityForMonth>) {
  if (status === 'not_installed') return 'Not installed';
  return status[0].toUpperCase() + status.slice(1);
}

function formatTimestamp(value?: string) {
  if (!value) {
    return 'Unknown';
  }
  return new Intl.DateTimeFormat('en-IL', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Jerusalem',
  }).format(new Date(value));
}


function coveragePercentValue(summary?: DashboardSummary) {
  const eligible = summary?.eligible_partitions ?? 0;
  return eligible === 0 ? 0 : Math.round(((summary?.completed_eligible_partitions ?? 0) / eligible) * 100);
}

function coveragePercent(summary?: DashboardSummary) {
  return `${coveragePercentValue(summary)}%`;
}




function parseMachineIds(value: string) {
  return value.split(/[\s,]+/).map((machineId) => machineId.trim()).filter(Boolean);
}

function detailMachineHref(machineId: string) {
  const url = new URL(window.location.href);
  url.searchParams.set('view', 'detail');
  url.searchParams.set('machine', machineId);
  return url.toString();
}

function monitorHref() {
  const url = new URL(window.location.href);
  url.searchParams.delete('view');
  url.searchParams.delete('machine');
  return url.toString();
}

function buildSandboxManifestPath(machineId: string, since: string, until: string) {
  const cleanedMachine = machineId.trim() || 'machine-id';
  const sinceLabel = compactDateLabel(since);
  const untilLabel = compactDateLabel(until);
  return `sandbox/dashboard/single-machine/${cleanedMachine}_${sinceLabel}_${untilLabel}.parquet`;
}

function buildSandboxTriggerCommand({
  environment,
  namespace,
  manifestPath,
  includeFeatures,
  features,
  maxParallelSteps,
}: {
  environment: EnvironmentTarget;
  namespace: string;
  manifestPath: string;
  includeFeatures: boolean;
  features: string;
  maxParallelSteps: number;
}) {
  const targetNamespace = environment === 'prod' ? PROD_CONTAINER : namespace;
  const featureList = splitFeatures(features);
  const parts = [
    'python FSTBackfill_prod_flow.py --no-pylint --with retry argo-workflows trigger',
    `--name-space ${targetNamespace || DEFAULT_DEV_NAMESPACE}`,
    `--storage_account_manifest_path ${manifestPath}`,
    `--manifest_bucket_count ${Math.max(maxParallelSteps, 1)}`,
  ];
  if (includeFeatures && featureList.length) {
    parts.push(`--features_to_backfill ${featureList.join(',')}`);
  }
  return parts.join(' ');
}

function compactDateLabel(value: string) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return 'yyyymmdd';
  }
  return parsed.toISOString().slice(0, 10).replaceAll('-', '');
}

function buildTriggerParams(trigger: TriggerForm, productionConfirmation = PROD_CONFIRMATION) {
  return {
    ...trigger,
    confirmation_text: trigger.environment === 'prod' && trigger.confirm_production ? productionConfirmation : '',
    features_to_backfill: trigger.include_features_to_backfill
      ? splitFeatures(trigger.features_to_backfill)
      : [],
  };
}

function splitFeatures(value: string) {
  return value.split(',').map((feature) => feature.trim()).filter(Boolean);
}

function isMonthBackfillable(month: MonthStatus) {
  const activity = month.activity_status ?? (typeof month.row_count === 'number' && month.row_count > 0 ? 'online' : 'unknown');
  return activity === 'online' && (month.status === 'needs_backfill' || month.status === 'backfilled');
}

function runnerCalendarMonthsInRange(since: string, until: string): number[] {
  const sinceDay = dateOnlyValue(since);
  const untilDay = dateOnlyValue(until);
  if (!sinceDay || !untilDay) return [];
  const start = new Date(`${sinceDay}T00:00:00`).getTime();
  const end = new Date(`${untilDay}T23:59:59`).getTime();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return [];
  return orchestratorMonthsThroughToday().filter(({ year, month }) => {
    const monthStart = new Date(year, month - 1, 1).getTime();
    const nextMonthStart = new Date(year, month, 1).getTime();
    return monthStart <= end && nextMonthStart > start;
  }).map(({ index }) => index);
}

function buildRunnerMonthSelection(
  machines: MachineStatus[],
  machineIds: string[],
  mode: 'range' | 'gaps' | 'all' | 'manual',
  since: string,
  until: string,
  manualIndices: number[],
): { plan: Record<string, number[]>; availability: Record<number, number>; skippedCount: number; candidateCount: number } {
  const selectedMachines = machineIds.flatMap((machineId) => {
    const machine = machines.find((item) => item.machine_id === machineId);
    return machine ? [machine] : [];
  });
  const rangeIndices = mode === 'range' ? new Set(runnerCalendarMonthsInRange(since, until)) : null;
  const manualSet = mode === 'manual' ? new Set(manualIndices) : null;
  const supportedIndices = new Set(orchestratorMonthsThroughToday().map(({ index }) => index));
  const availability: Record<number, number> = {};
  for (const machine of selectedMachines) {
    for (const month of machine.months) {
      if (!isMonthBackfillable(month)) continue;
      const index = orchestratorMonthIndex(month.partition);
      if (!supportedIndices.has(index)) continue;
      availability[index] = (availability[index] ?? 0) + 1;
    }
  }

  const plan: Record<string, number[]> = {};
  let candidateCount = 0;
  for (const machineId of machineIds) {
    const machine = selectedMachines.find((item) => item.machine_id === machineId);
    if (!machine) continue;
    const indices: number[] = [];
    for (const month of machine.months) {
      const index = orchestratorMonthIndex(month.partition);
      if (!supportedIndices.has(index)) continue;
      const statusAllowed = mode === 'gaps'
        ? month.status === 'needs_backfill'
        : month.status === 'needs_backfill' || month.status === 'backfilled';
      if (mode === 'range') {
        if (rangeIndices?.has(index)) candidateCount += 1;
      } else if (mode === 'manual') {
        // The manual candidate total is counted across the selected machines below.
      } else if (statusAllowed) {
        candidateCount += 1;
      }
      const chosenByMode = mode === 'range'
        ? Boolean(rangeIndices?.has(index))
        : mode === 'manual'
          ? Boolean(manualSet?.has(index))
          : statusAllowed;
      if (chosenByMode && isMonthBackfillable(month)) indices.push(index);
    }
    if (indices.length) plan[machineId] = [...new Set(indices)].sort((left, right) => left - right);
  }
  if (mode === 'range') candidateCount = runnerCalendarMonthsInRange(since, until).length * machineIds.length;
  if (mode === 'manual') candidateCount = manualIndices.length * machineIds.length;
  const selectedCount = Object.values(plan).reduce((total, indices) => total + indices.length, 0);
  return { plan, availability, skippedCount: Math.max(0, candidateCount - selectedCount), candidateCount };
}

function backfillRangesForMonths(months: MonthStatus[]) {
  const sorted = [...months].sort((left, right) => left.partition.index - right.partition.index);
  const groups: MonthStatus[][] = [];
  for (const month of sorted) {
    const previous = groups.at(-1)?.at(-1);
    if (previous && month.partition.index === previous.partition.index + 1) {
      groups.at(-1)!.push(month);
    } else {
      groups.push([month]);
    }
  }
  return groups.map((group) => {
    const first = group[0].partition;
    const last = group.at(-1)!.partition;
    return {
      since: `${first.year}-${String(first.month).padStart(2, '0')}-01T00:00:00`,
      until: backfillMonthUntil(last.year, last.month),
    };
  });
}

function defaultBackfillRange(machine: MachineStatus) {
  const month = machine.months.find((candidate) => candidate.status === 'needs_backfill' && isMonthBackfillable(candidate))
    ?? machine.months.find(isMonthBackfillable);
  if (!month) return null;
  const year = month.partition.year;
  const monthValue = month.partition.month;
  return {
    since: `${year}-${String(monthValue).padStart(2, '0')}-01T00:00:00`,
    until: backfillMonthUntil(year, monthValue),
  };
}

export default App;
