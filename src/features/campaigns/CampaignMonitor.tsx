import { useEffect, useState } from 'react';
import { API_BASE } from '../../constants';
import { campaignAction, getCampaign } from './api';
import type { Campaign } from './types';

export function CampaignMonitor({ initial, workflowMutationsEnabled = false }: { initial: Campaign; workflowMutationsEnabled?: boolean }) {
  const [campaign, setCampaign] = useState(initial);
  const [error, setError] = useState('');
  useEffect(() => {
    const events = new EventSource(`${API_BASE}/api/v1/backfill-campaigns/${encodeURIComponent(initial.id)}/events`);
    const refresh = () => getCampaign(initial.id).then(setCampaign).catch((value: unknown) => setError(value instanceof Error ? value.message : String(value)));
    ['work_completed', 'work_submitted', 'work_submit_failed', 'campaign_pause', 'campaign_resume', 'campaign_cancel', 'campaign_auto_paused', 'machine_pause', 'machine_resume', 'machine_cancel']
      .forEach((name) => events.addEventListener(name, refresh));
    let delay = 5000; let timer = 0;
    const fallback = () => { if (!document.hidden) void refresh(); delay = Math.min(30000, delay * 1.5); timer = window.setTimeout(fallback, delay); };
    events.onerror = () => { events.close(); fallback(); };
    return () => { events.close(); window.clearTimeout(timer); };
  }, [initial.id]);
  const act = (action: string) => { if (!workflowMutationsEnabled) return; campaignAction(campaign.id, action).then(setCampaign).catch((value: unknown) => setError(value instanceof Error ? value.message : String(value))); };
  const terminal = ['completed', 'failed', 'cancelled'].includes(campaign.state);
  return <section className="campaign-stage campaign-monitor"><div className="stage-title"><span>4</span><div><h3>{campaign.name}</h3><p className="mono">{campaign.id}</p></div></div>
    <div className="campaign-state-line"><span className={`campaign-state ${campaign.state}`}>{campaign.state}</span>{Object.entries(campaign.work_item_counts).map(([state, count]) => <span key={state}>{state}: {count.toLocaleString()}</span>)}</div>
    {error ? <p className="estimate-warning">{error}</p> : null}<div className="campaign-actions">
      {campaign.state === 'paused' ? <button type="button" disabled={!workflowMutationsEnabled} onClick={() => void act('resume')}>Resume</button> : !terminal ? <button type="button" disabled={!workflowMutationsEnabled} onClick={() => void act('pause')}>Pause</button> : null}
      {!terminal ? <button className="danger" type="button" disabled={!workflowMutationsEnabled} onClick={() => void act('cancel')}>Cancel</button> : null}
    </div>
  </section>;
}
