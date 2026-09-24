import type { InventoryPage } from './types';

type Props = { page: InventoryPage | null; loading: boolean; selected: Set<string>; selectAll: boolean;
  onToggle: (id: string) => void; onSelectAll: (value: boolean) => void };
export function MachineInventoryTable({ page, loading, selected, selectAll, onToggle, onSelectAll }: Props) {
  return <><div className="selection-toolbar">
    <label><input type="checkbox" checked={selectAll} onChange={(event) => onSelectAll(event.target.checked)} /> Select all matching</label>
    <span>{selectAll ? `${page?.total_estimate?.toLocaleString() ?? 'All'} matching` : `${selected.size} explicitly selected`}</span>
  </div><div className="inventory-table-wrap campaign-table"><table><thead><tr><th /><th>Machine</th><th>Cohort</th><th>Endpoints</th><th>Eligible</th><th>Reason</th></tr></thead><tbody>
    {loading ? Array.from({ length: 6 }, (_, index) => <tr className="inventory-skeleton" key={index}><td colSpan={6}><i /></td></tr>) : null}
    {!loading && page?.machines.map((machine) => <tr key={machine.machine_id} className={!machine.backfill_eligible ? 'excluded-row' : ''}>
      <td><input type="checkbox" disabled={selectAll || !machine.backfill_eligible} checked={selectAll ? machine.backfill_eligible : selected.has(machine.machine_id)} onChange={() => onToggle(machine.machine_id)} /></td>
      <td><strong>{machine.display_name || machine.machine_id}</strong><small className="mono machine-subtitle">{machine.machine_id}</small></td>
      <td><span className={`cohort-pill ${machine.resource_cohort}`}>{machine.resource_cohort}</span></td><td>{machine.endpoint_count}</td>
      <td>{machine.backfill_eligible ? 'Yes' : 'No'}</td><td>{machine.classification_reason.join(', ')}</td>
    </tr>)}</tbody></table></div></>;
}
