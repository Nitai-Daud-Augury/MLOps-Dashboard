import type { Estimate, EstimateLane } from './types';

const range = (values: Array<number | null>, suffix: string) => values.every((value) => value == null)
  ? 'Unavailable' : values.map((value) => value == null ? '—' : `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}${suffix}`).join(' / ');

function Lane({ lane }: { lane: EstimateLane }) {
  return <article className={`estimate-lane ${lane.cohort}`}><header><span className={`cohort-pill ${lane.cohort}`}>{lane.cohort}</span><strong>{lane.machine_count.toLocaleString()} machines</strong></header>
    <dl><div><dt>Windows</dt><dd>{lane.work_item_count.toLocaleString()}</dd></div><div><dt>Concurrency</dt><dd>{lane.resource_profile.recommended_concurrency}</dd></div>
      <div><dt>Time low / likely / high</dt><dd>{range(lane.completion_hours, 'h')}</dd></div><div><dt>Allocated cost</dt><dd>{range(lane.allocated_cost, ' USD')}</dd></div>
      <div><dt>Node billing estimate</dt><dd>{range(lane.node_cost, ' USD')}</dd></div><div><dt>Storage transactions</dt><dd>{lane.storage_cost == null ? 'Unknown · excluded' : `${lane.storage_cost} USD`}</dd></div>
      <div><dt>Worker</dt><dd>{(lane.resource_profile.memory_mib / 1024).toFixed(0)} GiB · {lane.resource_profile.pool}</dd></div><div><dt>Confidence</dt><dd>{lane.confidence}</dd></div></dl>
    {lane.blocked ? <p className="estimate-warning">Capacity is zero; submission is blocked.</p> : null}
  </article>;
}

export function EstimateReview({ estimate, submitting, production, onSubmit, submitDisabled = false }: { estimate: Estimate; submitting: boolean; production: boolean; onSubmit: () => void; submitDisabled?: boolean }) {
  const total = estimate.lanes.standard.machine_count + estimate.lanes.ulrpm.machine_count;
  return <section className="campaign-stage"><div className="stage-title"><span>3</span><div><h3>Review immutable estimate</h3><p>{estimate.disclaimer}</p></div></div>
    <div className="estimate-summary"><strong>{total.toLocaleString()} selected</strong><span>{estimate.excluded_unknown_or_ineligible.toLocaleString()} excluded</span><span>Expires {new Date(estimate.expires_at).toLocaleTimeString()}</span></div>
    <div className="estimate-lanes"><Lane lane={estimate.lanes.standard} /><Lane lane={estimate.lanes.ulrpm} /></div>
    {production ? <p className="production-count-confirmation">This production submission binds exactly <strong>{estimate.lanes.standard.machine_count.toLocaleString()} standard</strong>, <strong>{estimate.lanes.ulrpm.machine_count.toLocaleString()} ULRPM</strong>, and <strong>{estimate.excluded_unknown_or_ineligible.toLocaleString()} excluded/unknown</strong> machines.</p> : null}
    {!estimate.production_ready ? <p className="estimate-warning">Production is locked until Mongo, capacity, pricing, signing, and benchmark evidence are verified. Development submission remains available.</p> : null}
    <button className="primary-button campaign-submit" type="button" disabled={submitDisabled || submitting || (production && !estimate.production_ready)} title={submitDisabled ? 'Workflow operations are disabled in monitor-only runtime' : undefined} onClick={onSubmit}>{submitting ? 'Submitting…' : submitDisabled ? 'Submission unavailable in monitor-only mode' : production && !estimate.production_ready ? 'Production verification required' : 'Confirm exact estimate and queue'}</button>
  </section>;
}
