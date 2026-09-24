import { useEffect, useMemo, useState } from 'react';
import {
  Brush,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { AlertTriangle, ChevronDown, RefreshCw } from 'lucide-react';
import { API_BASE } from '../../constants';
import type { MachineStatus } from '../../types';

type ChartMode = 'raw' | 'scaled' | 'ratio';

interface FeaturePoint {
  recorded_at: string;
  group_key: string;
  v1: number | null;
  v2: number | null;
  ratio: number | null;
}

interface FeatureGroup {
  key: string;
  label: string;
  component_id: string | null;
  bearing: number | null;
  plane: number | null;
  row_count: number;
}

interface FeatureSeriesResponse {
  machine_id: string;
  month: string;
  source_account: string;
  source_container: string;
  blob_url: string;
  available_features: string[];
  feature: string;
  v2_feature: string;
  row_count: number;
  sampled_count: number;
  v1_non_null: number;
  v2_non_null: number;
  paired_count: number;
  median_ratio: number | null;
  p05_ratio: number | null;
  p95_ratio: number | null;
  range_start: string | null;
  range_end: string | null;
  groups?: FeatureGroup[];
  points: FeaturePoint[];
}

const FEATURE_LABELS: Record<string, string> = {
  ultrasonic_p2p: 'Peak to peak',
  ultrasonic_rms: 'RMS',
  ultrasonic_mad: 'MAD',
  ultrasonic_hf_rms: 'High-frequency RMS',
  ultrasonic_rms_30_35: 'RMS 30–35 kHz',
  ultrasonic_rms_35_40: 'RMS 35–40 kHz',
  ultrasonic_rms_40_45: 'RMS 40–45 kHz',
  ultrasonic_impact_energy: 'Impact energy',
};
const GROUP_COLORS = ['#00a3bf', '#ff8f00', '#00c853', '#9575cd', '#ef5350', '#42a5f5', '#d4e157', '#ec407a'];

export function MachineFeatureChart({ machine }: { machine: MachineStatus }) {
  const readableMonths = useMemo(
    () => machine.months.filter((item) => (item.row_count ?? 0) > 0).slice().reverse(),
    [machine.months],
  );
  const [monthLabel, setMonthLabel] = useState(() => readableMonths[0] ? partitionLabel(readableMonths[0].partition) : '');
  const [feature, setFeature] = useState('ultrasonic_p2p');
  const [mode, setMode] = useState<ChartMode>('raw');
  const [series, setSeries] = useState<FeatureSeriesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [planesOpen, setPlanesOpen] = useState(true);
  const [selectedGroups, setSelectedGroups] = useState<string[]>([]);

  useEffect(() => {
    if (readableMonths.some((item) => partitionLabel(item.partition) === monthLabel)) return;
    setMonthLabel(readableMonths[0] ? partitionLabel(readableMonths[0].partition) : '');
  }, [machine.machine_id, monthLabel, readableMonths]);

  useEffect(() => {
    if (!monthLabel) return;
    const selectedMonth = readableMonths.find((item) => partitionLabel(item.partition) === monthLabel);
    if (!selectedMonth) return;
    const { year, month } = selectedMonth.partition;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetch(`${API_BASE}/api/machines/${encodeURIComponent(machine.machine_id)}/feature-series?year=${year}&month=${month}&feature=${encodeURIComponent(feature)}&max_points=2500`, { signal: controller.signal })
      .then(async (response) => {
        if (response.ok) return response.json() as Promise<FeatureSeriesResponse>;
        const body = await response.json().catch(() => ({})) as { detail?: string };
        throw new Error(apiErrorMessage(body.detail, response.status));
      })
      .then((payload) => {
        if (!Array.isArray(payload.groups)) {
          throw new Error('Restart the dashboard API to enable plane filtering.');
        }
        const groups = payload.groups;
        setSeries(payload);
        setSelectedGroups((current) => {
          const available = new Set(groups.map((group) => group.key));
          const retained = current.filter((key) => available.has(key));
          return retained.length ? retained : groups.map((group) => group.key);
        });
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return;
        setSeries(null);
        setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [feature, machine.machine_id, monthLabel, readableMonths, reloadKey]);

  const selectedPoints = useMemo(() => (series?.points ?? []).filter((point) => selectedGroups.includes(point.group_key)), [selectedGroups, series]);
  const selectedRatios = useMemo(() => selectedPoints.flatMap((point) => point.ratio == null ? [] : [point.ratio]), [selectedPoints]);
  const selectedMedianRatio = percentile(selectedRatios, .5);
  const chartData = useMemo(() => {
    const groupIndexes = new Map((series?.groups ?? []).map((group, index) => [group.key, index]));
    const byTimestamp = new Map<string, Record<string, string | number | null>>();
    selectedPoints.forEach((point) => {
      const groupIndex = groupIndexes.get(point.group_key);
      if (groupIndex == null) return;
      const row = byTimestamp.get(point.recorded_at) ?? { recorded_at: point.recorded_at };
      row[`g${groupIndex}_v1`] = point.v1;
      row[`g${groupIndex}_v2`] = point.v2;
      row[`g${groupIndex}_scaled`] = point.v1 != null && selectedMedianRatio != null ? point.v1 * selectedMedianRatio : null;
      row[`g${groupIndex}_ratio`] = point.ratio;
      byTimestamp.set(point.recorded_at, row);
    });
    return [...byTimestamp.values()].sort((left, right) => String(left.recorded_at).localeCompare(String(right.recorded_at)));
  }, [selectedMedianRatio, selectedPoints, series?.groups]);
  const groupedPlanes = useMemo(() => groupPlaneCards(series?.groups ?? []), [series?.groups]);
  const availableGroups = series?.groups ?? [];

  if (!readableMonths.length) {
    return <section className="panel feature-chart-panel"><div className="feature-chart-empty"><AlertTriangle size={20} /><strong>No chartable partitions</strong><span>This machine has no scanned month with Feature Store rows.</span></div></section>;
  }

  return <section className="panel feature-chart-panel">
    <div className="panel-header feature-chart-header">
      <div><p className="eyebrow">Feature values</p><h2>Ultrasonic v1 vs v2</h2><p>Compare the recorded values in one monthly FST partition. Drag the timeline handles to zoom into a smaller window.</p></div>
      <button className="icon-button" type="button" onClick={() => setReloadKey((key) => key + 1)} disabled={loading} title="Reload feature values"><RefreshCw className={loading ? 'spin' : ''} size={17} /></button>
    </div>

    <div className="feature-chart-controls">
      <label><span>Month</span><select value={monthLabel} onChange={(event) => setMonthLabel(event.target.value)}>{readableMonths.map((item) => { const label = partitionLabel(item.partition); return <option key={label} value={label}>{label} · {item.status.replace('_', ' ')}</option>; })}</select></label>
      <label><span>Feature</span><select value={feature} onChange={(event) => setFeature(event.target.value)}>{Object.entries(FEATURE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <div className="feature-chart-mode" role="group" aria-label="Chart display mode">
        {(['raw', 'scaled', 'ratio'] as ChartMode[]).map((item) => <button className={mode === item ? 'active' : ''} key={item} type="button" onClick={() => setMode(item)}>{item === 'raw' ? 'Raw values' : item === 'scaled' ? 'Scaled overlay' : 'v2 / v1 ratio'}</button>)}
      </div>
    </div>

    {!loading && !error && availableGroups.length ? <section className="plane-filter">
      <button className="plane-filter-heading" type="button" onClick={() => setPlanesOpen((open) => !open)} aria-expanded={planesOpen}>
        <ChevronDown className={planesOpen ? '' : 'collapsed'} size={17} />
        <span>Planes</span>
        <strong>{selectedGroups.length} of {availableGroups.length} selected</strong>
      </button>
      {planesOpen ? <div className="plane-filter-body">
        <div className="plane-filter-actions"><span>Choose the positions used for the production v1 and test v2 comparison.</span><div><button type="button" onClick={() => setSelectedGroups(availableGroups.map((group) => group.key))}>Select all</button><button type="button" onClick={() => setSelectedGroups([])}>Clear</button></div></div>
        <div className="plane-card-grid">{groupedPlanes.map((card) => <article className="plane-card" key={card.key}><div><strong>{card.componentLabel}</strong><span>Bearing {card.bearing}</span></div><div>{card.groups.map((group) => { const active = selectedGroups.includes(group.key); return <button className={active ? 'active' : ''} key={group.key} type="button" aria-pressed={active} title={group.label} onClick={() => setSelectedGroups((current) => active ? current.filter((key) => key !== group.key) : [...current, group.key])}>{planeLabel(group.plane)}</button>; })}</div></article>)}</div>
      </div> : null}
    </section> : null}

    {error ? <div className="feature-chart-error"><AlertTriangle size={18} /><div><strong>Feature values are unavailable</strong><span>{error}</span></div></div> : null}
    {loading ? <div className="feature-chart-loading"><RefreshCw className="spin" size={20} /><span>Reading {monthLabel} from Feature Store…</span></div> : null}
    {!loading && !error && series ? <>
      <div className="feature-chart-stats">
        <Stat label="Visible rows" value={formatInteger(selectedPoints.length)} detail={`${selectedGroups.length} plane${selectedGroups.length === 1 ? '' : 's'} selected`} />
        <Stat label="Production v1" value={formatInteger(selectedPoints.filter((point) => point.v1 != null).length)} detail={series.feature} />
        <Stat label="Test v2" value={formatInteger(selectedPoints.filter((point) => point.v2 != null).length)} detail={series.v2_feature} />
        <Stat label="Median ratio" value={formatValue(selectedMedianRatio)} detail={`p05 ${formatValue(percentile(selectedRatios, .05))} · p95 ${formatValue(percentile(selectedRatios, .95))}`} />
      </div>
      <div className="feature-chart-canvas" aria-label={`${FEATURE_LABELS[feature]} ${mode} chart for ${monthLabel}`}>
        <ResponsiveContainer width="100%" height={460}>
          <LineChart data={chartData} margin={{ top: 18, right: mode === 'raw' ? 34 : 18, left: 4, bottom: 6 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 5" vertical={false} />
            <XAxis dataKey="recorded_at" minTickGap={55} tickFormatter={formatAxisTime} stroke="var(--text-muted)" tick={{ fontSize: 11 }} />
            <YAxis yAxisId="left" stroke="var(--text-muted)" tick={{ fontSize: 11 }} tickFormatter={formatCompact} width={64} />
            {mode === 'raw' ? <YAxis yAxisId="right" orientation="right" stroke="var(--warning)" tick={{ fontSize: 11 }} tickFormatter={formatCompact} width={64} /> : null}
            <Tooltip labelFormatter={(label) => formatTimestamp(String(label))} formatter={(value, name) => [formatValue(typeof value === 'number' ? value : null), String(name)]} contentStyle={{ background: 'var(--bg-surface)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text-primary)' }} />
            <Legend />
            {availableGroups.map((group, index) => selectedGroups.includes(group.key) ? <GroupLines group={group} index={index} mode={mode} medianRatio={selectedMedianRatio} key={group.key} /> : null)}
            <Brush dataKey="recorded_at" height={28} travellerWidth={9} tickFormatter={formatAxisTime} stroke="var(--accent)" fill="var(--bg-surface-alt)" />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="feature-chart-footer"><span>{formatTimestamp(series.range_start)} — {formatTimestamp(series.range_end)}</span><a href={series.blob_url} target="_blank" rel="noreferrer">{series.source_account}/{series.source_container}</a></div>
    </> : null}
  </section>;
}

function Stat({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <article><span>{label}</span><strong>{value}</strong><small title={detail}>{detail}</small></article>;
}

function GroupLines({ group, index, mode, medianRatio }: { group: FeatureGroup; index: number; mode: ChartMode; medianRatio: number | null }) {
  const color = GROUP_COLORS[index % GROUP_COLORS.length];
  const shortLabel = `${planeLabel(group.plane)} · B${group.bearing ?? '?'}`;
  if (mode === 'raw') return <><Line yAxisId="left" type="monotone" dataKey={`g${index}_v1`} name={`${shortLabel} production v1`} stroke={color} dot={false} strokeWidth={1.7} connectNulls /><Line yAxisId="right" type="monotone" dataKey={`g${index}_v2`} name={`${shortLabel} test v2`} stroke={color} strokeDasharray="6 4" dot={false} strokeWidth={1.7} connectNulls /></>;
  if (mode === 'scaled') return <><Line yAxisId="left" type="monotone" dataKey={`g${index}_scaled`} name={`${shortLabel} production × ${formatValue(medianRatio)}`} stroke={color} dot={false} strokeWidth={1.7} connectNulls /><Line yAxisId="left" type="monotone" dataKey={`g${index}_v2`} name={`${shortLabel} test v2`} stroke={color} strokeDasharray="6 4" dot={false} strokeWidth={1.7} connectNulls /></>;
  return <Line yAxisId="left" type="monotone" dataKey={`g${index}_ratio`} name={`${shortLabel} v2 / v1`} stroke={color} dot={false} strokeWidth={1.8} connectNulls />;
}

function groupPlaneCards(groups: FeatureGroup[]) {
  const cards = new Map<string, { key: string; componentLabel: string; bearing: number | null; groups: FeatureGroup[] }>();
  groups.forEach((group) => {
    const key = `${group.component_id ?? 'unknown'}|${group.bearing ?? 'unknown'}`;
    const suffix = group.component_id ? `…${group.component_id.slice(-6)}` : 'Unknown';
    const card = cards.get(key) ?? { key, componentLabel: `Component ${suffix}`, bearing: group.bearing, groups: [] };
    card.groups.push(group);
    cards.set(key, card);
  });
  return [...cards.values()];
}

function planeLabel(plane: number | null) { return plane === 0 ? 'R1' : plane === 1 ? 'R2' : plane === 2 ? 'A' : plane == null ? 'All' : `P${plane}`; }
function percentile(values: number[], position: number) {
  if (!values.length) return null;
  const ordered = [...values].sort((left, right) => left - right);
  const index = (ordered.length - 1) * position;
  const lower = Math.floor(index);
  const upper = Math.ceil(index);
  return lower === upper ? ordered[lower] : ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower);
}

function formatInteger(value: number) { return new Intl.NumberFormat('en-US').format(value); }
function partitionLabel(partition: { year: number; month: number }) { return `${partition.year}-${String(partition.month).padStart(2, '0')}`; }
function apiErrorMessage(detail: unknown, status: number) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => {
      if (item && typeof item === 'object' && 'msg' in item && typeof item.msg === 'string') return item.msg;
      return null;
    }).filter((item): item is string => Boolean(item));
    if (messages.length) return messages.join('; ');
  }
  return `Could not load feature values (${status})`;
}
function formatValue(value: number | null) {
  if (value == null || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: Math.abs(value) < 1 ? 4 : 2 }).format(value);
}
function formatCompact(value: number) { return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(value); }
function formatTimestamp(value: string | null) { return value ? new Date(value).toLocaleString() : 'Unknown'; }
function formatAxisTime(value: string) { return new Date(value).toLocaleDateString('en', { month: 'short', day: 'numeric' }); }
