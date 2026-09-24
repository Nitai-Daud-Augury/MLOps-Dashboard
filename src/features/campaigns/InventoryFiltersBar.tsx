import type { InventoryFilters } from './types';

export function InventoryFiltersBar({ filters, onChange }: { filters: InventoryFilters; onChange: (value: InventoryFilters) => void }) {
  const update = (key: keyof InventoryFilters, value: string) => onChange({ ...filters, [key]: value });
  return <div className="inventory-filters campaign-filters">
    <input value={filters.search} onChange={(event) => update('search', event.target.value)} placeholder="Search machine or name" />
    <select value={filters.cohort} onChange={(event) => update('cohort', event.target.value)}><option value="">All cohorts</option><option value="standard">Standard</option><option value="ulrpm">ULRPM</option><option value="unknown">Unknown</option></select>
    <select value={filters.status} onChange={(event) => update('status', event.target.value)}><option value="">All statuses</option><option value="active">Active</option><option value="deactivated">Deactivated</option></select>
    <select value={filters.eligible} onChange={(event) => update('eligible', event.target.value)}><option value="">All eligibility</option><option value="true">Eligible</option><option value="false">Excluded</option></select>
    <input value={filters.site_id} onChange={(event) => update('site_id', event.target.value)} placeholder="Site ID" />
    <input value={filters.organization_id} onChange={(event) => update('organization_id', event.target.value)} placeholder="Organization ID" />
    <select value={filters.classification_issue} onChange={(event) => update('classification_issue', event.target.value)}><option value="">All classification results</option><option value="unrecognized_hardware">Unknown hardware</option><option value="no_endpoints">No endpoints</option><option value="malformed">Malformed configuration</option></select>
    <select value={filters.sort_by} onChange={(event) => update('sort_by', event.target.value)}><option value="machine_id">Sort: machine ID</option><option value="display_name">Sort: name</option><option value="site_name">Sort: site</option><option value="organization_name">Sort: organization</option><option value="resource_cohort">Sort: cohort</option></select>
    <button type="button" onClick={() => update('sort_dir', filters.sort_dir === 'asc' ? 'desc' : 'asc')}>{filters.sort_dir === 'asc' ? 'Ascending' : 'Descending'}</button>
  </div>;
}
