import { useEffect, useState } from 'react';
import { listCampaigns } from './api';
import { CampaignMonitor } from './CampaignMonitor';
import type { Campaign } from './types';

export function RecentCampaigns({ active, workflowMutationsEnabled = false }: { active: Campaign | null; workflowMutationsEnabled?: boolean }) {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [opened, setOpened] = useState<Campaign | null>(null);
  useEffect(() => { void listCampaigns().then((value) => setCampaigns(value.campaigns)).catch(() => setCampaigns([])); }, [active?.id]);
  if (active) return <CampaignMonitor initial={active} workflowMutationsEnabled={workflowMutationsEnabled} />;
  return <section className="campaign-stage recent-campaigns"><div className="stage-title"><span>4</span><div><h3>Recent campaigns</h3><p>Resume monitoring after a browser refresh.</p></div></div>
    {opened ? <><button type="button" onClick={() => setOpened(null)}>Back to list</button><CampaignMonitor initial={opened} workflowMutationsEnabled={workflowMutationsEnabled} /></> : campaigns.length ? <div className="recent-campaign-list">{campaigns.map((campaign) => <button type="button" key={campaign.id} onClick={() => setOpened(campaign)}><strong>{campaign.name}</strong><span className={`campaign-state ${campaign.state}`}>{campaign.state}</span><small>{Object.values(campaign.work_item_counts).reduce((sum, value) => sum + value, 0).toLocaleString()} windows</small></button>)}</div> : <p className="inventory-note">No campaigns have been planned yet.</p>}
  </section>;
}
