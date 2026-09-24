export type CampaignConfig = {
  name: string; startAt: string; endAt: string; featureVersion: string;
  standardWindowDays: number; ulrpmWindowDays: number; production: boolean;
  confirmation: string; ulrpmConfirmation: string;
};
type Props = { value: CampaignConfig; onChange: (value: CampaignConfig) => void };

export function CampaignConfiguration({ value, onChange }: Props) {
  const set = <K extends keyof CampaignConfig>(key: K, field: CampaignConfig[K]) => onChange({ ...value, [key]: field });
  return <section className="campaign-stage"><div className="stage-title"><span>2</span><div><h3>Configure windows</h3><p>Each machine lane stays chronological; cohorts run separately.</p></div></div>
    <div className="campaign-form-grid">
      <label>Campaign name<input value={value.name} onChange={(event) => set('name', event.target.value)} /></label>
      <label>Feature/config version<input value={value.featureVersion} onChange={(event) => set('featureVersion', event.target.value)} /></label>
      <label>Start date<input type="date" value={value.startAt} onChange={(event) => set('startAt', event.target.value)} /></label>
      <label>End date (exclusive)<input type="date" value={value.endAt} onChange={(event) => set('endAt', event.target.value)} /></label>
      <label>Standard window<input type="number" min={1} max={31} value={value.standardWindowDays} onChange={(event) => set('standardWindowDays', Number(event.target.value))} /><small>days · 8 GiB lane</small></label>
      <label>ULRPM window<input type="number" min={1} max={7} value={value.ulrpmWindowDays} onChange={(event) => set('ulrpmWindowDays', Number(event.target.value))} /><small>days · 64 GiB lane</small></label>
    </div>
    <label className="production-toggle"><input type="checkbox" checked={value.production} onChange={(event) => set('production', event.target.checked)} /> Production campaign</label>
    {value.production ? <div className="production-confirmations"><label>Type RUN_PROD_BACKFILL<input value={value.confirmation} onChange={(event) => set('confirmation', event.target.value)} /></label><label>For ULRPM spend, type APPROVE_ULRPM_ON_DEMAND<input value={value.ulrpmConfirmation} onChange={(event) => set('ulrpmConfirmation', event.target.value)} /></label></div> : null}
  </section>;
}
