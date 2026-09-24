import { ExternalLink, Loader2, RefreshCw } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { API_BASE } from '../../constants';

const HISTORY_URL = 'https://ui.augury.obp.outerbounds.com/dashboard/costreporting/history';
interface CostReport { available: boolean; detail?: string; history_url?: string; report?: unknown; }

export function CurrentCostPanel() {
  const [data, setData] = useState<CostReport | null>(null);
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    setLoading(true);
    try { const response = await fetch(`${API_BASE}/api/outerbounds/cost-report`); setData(await response.json() as CostReport); }
    catch (error) { setData({ available: false, detail: error instanceof Error ? error.message : String(error) }); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  return <section className="current-cost-panel admin-view-panel cost-panel">
    <header className="panel-header"><div><p className="eyebrow">Outerbounds</p><h2>Current cost reporting</h2><p>Read-only cost data from the Outerbounds history report.</p></div><div className="actions"><button className="icon-button" type="button" onClick={() => void load()} disabled={loading} title="Refresh current costs"><RefreshCw className={loading ? 'spin' : ''} size={17} /></button><a className="secondary-button" href={data?.history_url ?? HISTORY_URL} target="_blank" rel="noreferrer"><ExternalLink size={16} />Open Outerbounds</a></div></header>
    {loading && !data ? <div className="empty-state"><Loader2 className="spin" size={24} />Loading cost report…</div> : data?.available ? <pre className="cost-report-json">{JSON.stringify(data.report, null, 2)}</pre> : <div className="cost-report-empty"><h3>Cost report not configured</h3><p>{data?.detail ?? 'The cost report could not be loaded.'}</p><p>The estimator remains available using Azure Retail pricing; configure the read-only Outerbounds JSON endpoint on the backend to populate this report.</p></div>}
  </section>;
}
