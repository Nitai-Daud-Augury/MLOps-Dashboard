import { useMemo, useState } from 'react';
import { AlertTriangle, Calculator } from 'lucide-react';
import { requestEstimate, submitCampaign } from './api';
import { CampaignConfiguration, type CampaignConfig } from './CampaignConfiguration';
import { EstimateReview } from './EstimateReview';
import { InventoryFiltersBar } from './InventoryFiltersBar';
import { MachineInventoryTable } from './MachineInventoryTable';
import { useInventory } from './useInventory';
import { ReadinessBanner } from './ReadinessBanner';
import { RecentCampaigns } from './RecentCampaigns';
import type { Campaign, Estimate, InventoryFilters } from './types';

const initialFilters: InventoryFilters = { search: '', cohort: '', status: '', eligible: 'true', site_id: '', organization_id: '', classification_issue: '', sort_by: 'machine_id', sort_dir: 'asc' };
const isoDay = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
const today = new Date(); const nextMonth = new Date(today); nextMonth.setDate(nextMonth.getDate() + 30);
const initialConfig: CampaignConfig = { name: '', startAt: isoDay(today), endAt: isoDay(nextMonth), featureVersion: 'current', standardWindowDays: 30, ulrpmWindowDays: 5, production: false, confirmation: '', ulrpmConfirmation: '' };

export function CampaignWorkspace({ workflowMutationsEnabled = false }: { workflowMutationsEnabled?: boolean }) {
  const [filters, setFilters] = useState(initialFilters); const [cursor, setCursor] = useState<string | null>(null);
  const [history, setHistory] = useState<string[]>([]); const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selectAll, setSelectAll] = useState(false); const [config, setConfig] = useState(initialConfig);
  const [estimate, setEstimate] = useState<Estimate | null>(null); const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const { page, loading, error: inventoryError } = useInventory(filters, cursor);
  const selection = useMemo(() => selectAll ? { inventory_version: page?.inventory_version ?? '', filter: { ...filters, search: filters.search || null, cohort: filters.cohort || null, status: filters.status || null, eligible: filters.eligible ? filters.eligible === 'true' : null, site_id: filters.site_id || null, organization_id: filters.organization_id || null, classification_issue: filters.classification_issue || null }, excluded_machine_ids: [] } : { inventory_version: page?.inventory_version ?? '', explicit_machine_ids: [...selected] }, [selectAll, page?.inventory_version, filters, selected]);
  const reset = () => { setEstimate(null); setCampaign(null); setError(''); };
  const changeFilters = (value: InventoryFilters) => { setFilters(value); setCursor(null); setHistory([]); setSelected(new Set()); setSelectAll(false); reset(); };
  const toggle = (id: string) => { const next = new Set(selected); next.has(id) ? next.delete(id) : next.add(id); setSelected(next); reset(); };
  const estimateRun = async () => { setBusy(true); try { setEstimate(await requestEstimate({ selection, start_at: config.startAt, end_at: config.endAt, feature_set_version: config.featureVersion, standard_window_days: config.standardWindowDays, ulrpm_window_days: config.ulrpmWindowDays })); setError(''); } catch (value) { setError(value instanceof Error ? value.message : String(value)); } finally { setBusy(false); } };
  const submit = async () => { if (!estimate || !workflowMutationsEnabled) return; setBusy(true); try { setCampaign(await submitCampaign({ estimate_id: estimate.estimate_id, estimate_signature: estimate.estimate_signature, name: config.name, production: config.production, confirmation_text: config.confirmation, ulrpm_confirmation_text: config.ulrpmConfirmation })); setError(''); } catch (value) { setError(value instanceof Error ? value.message : String(value)); } finally { setBusy(false); } };
  return <section className="panel inventory-panel campaign-workspace"><div className="panel-header"><div><p className="eyebrow">All-machine control plane</p><h2>Build a backfill campaign</h2><p>Paginated inventory, isolated resource lanes, bounded durable dispatch.</p></div></div>
    <ReadinessBanner />
    <section className="campaign-stage"><div className="stage-title"><span>1</span><div><h3>Select machines</h3><p>Unknown or ineligible machines stay visible but cannot be selected.</p></div></div><InventoryFiltersBar filters={filters} onChange={changeFilters} />
      {(error || inventoryError) ? <p className="inventory-error"><AlertTriangle size={16} />{error || inventoryError}</p> : null}<MachineInventoryTable page={page} loading={loading} selected={selected} selectAll={selectAll} onToggle={toggle} onSelectAll={(value) => { setSelectAll(value); setSelected(new Set()); reset(); }} />
      <div className="inventory-pagination"><button type="button" disabled={!history.length} onClick={() => { const copy = [...history]; setCursor(copy.pop() || null); setHistory(copy); }}>Previous</button><span>{page?.total_estimate?.toLocaleString() ?? 0} matching · inventory {page?.inventory_version ?? 'loading'}</span><button type="button" disabled={!page?.next_cursor} onClick={() => { if (page?.next_cursor) { setHistory([...history, cursor ?? '']); setCursor(page.next_cursor); } }}>Next</button></div>
    </section><CampaignConfiguration value={config} onChange={(value) => { setConfig(value); reset(); }} />
    {!estimate ? <button className="primary-button estimate-button" type="button" disabled={busy || (!selectAll && !selected.size)} onClick={() => void estimateRun()}><Calculator size={17} />{busy ? 'Estimating…' : 'Calculate safe time & cost'}</button> : <EstimateReview estimate={estimate} submitting={busy} production={config.production} onSubmit={() => void submit()} submitDisabled={!workflowMutationsEnabled} />}
    {!workflowMutationsEnabled ? <p className="inventory-note">Workflow submission and campaign controls are disabled in monitor-only runtime.</p> : null}
    <RecentCampaigns active={campaign} workflowMutationsEnabled={workflowMutationsEnabled} />
  </section>;
}
