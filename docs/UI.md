# Dashboard UI

The frontend was rebuilt from scratch on `feature/ui-rebuild` (spec:
`drift-dashboard-redesign-spec.md`, with Nitai's overrides). React 19 + Vite + React Router 7,
one hand-written stylesheet (`src/styles.css`, design tokens as HSL CSS variables), lucide icons.
No chart library, no component framework.

## Routes

Monitoring (blue chrome) and Admin (amber chrome + `ADMIN` ribbon) are separate route trees with
separate layouts (`src/layouts/MonitorLayout.tsx`, `src/layouts/AdminLayout.tsx`).

| Route | Screen |
|---|---|
| `/monitor` | Fleet coverage: last scanned / restored-saved-scan state, Refresh, Rescan (progress bar while running), KPI chips, search/status/hide-test filters and sort in the URL, paginated table, Export needs_backfill, quick-view sheet (`?quick=<id>`) |
| `/monitor/machines/:id` | Machine detail, tabs in the URL (`?tab=`): Overview, Coverage (text month grid), Running (pending + running workflows for the machine, rate-limited Refresh, active month locks), History (dashboard actions) |
| `/monitor/live` | Live Ops: snapshot version, fetched time, socket state, counts, Active+pending / Failed / Completed lists; pending dispatch rows are dashed with the GHA link |
| `/monitor/source` (old `/monitor/settings/source` redirects) | Features-source switch (full placement) with its persisted state, plus the blob account/container used by blob scans |
| `/admin` | Readiness checks, runtime mutation error (informational; buttons are never pre-disabled), recent actions |
| `/admin/backfill` | ① Scope (one / selected / all, lock status "Checking…" until `runner-lock-status` answers) ② months (gaps / all / range) + split + parallelism + environment ③ estimate review (machines, months, manifest windows, blockers) ④ Submit or Create manifests only ⑤ Track (stepper over the admin-action phases, cancel before dispatch) |
| `/admin/operations` | Running + pending workflows with Stop (type the workflow ID), cancel one month / stop the entire parent backfill, failures review |
| `/admin/diagnostics` | Outerbounds probe (stages with HTTP code, hints, counts) |

Old `/?view=detail&machine=<id>` links redirect to `/monitor/machines/<id>`. The backend's SPA
fallback (`static_host.py`) already serves `index.html` for these paths.

The features-source switch is `FeaturesSourceControl({ placement: 'header' | 'page', canSwitch })`:
in the Monitoring header and on `/monitor/source`, never in Admin. Switching opens a
confirm dialog (rescan required; "Switch only" or "Switch and rescan now"); the rescan banner
(`FeaturesSourceBanner`) collapses but stays until a scan from the new source completes.

## Feedback per phase

Skeleton rows on first load; stale-while-revalidate (cached data stays visible while reloading);
"Loading saved scan…" while the persisted scan restores after a restart and a
"restored after restart" chip afterwards; progress bar + toasts for scans; red banners with Retry
for errors; `RefreshButton` shows "Refreshing…" and the server's "Retry in Ns" countdown
(`refresh.state === 'limited'`); pending dispatch rows; the backfill Track stepper; toasts for
every action (errors stay until dismissed). Status is always icon + label.

## Future work (removed from the UI on purpose)

The backend endpoints below still exist; only the UI was dropped. Rebuild them on the new
components when needed.

- **Campaigns** (all-machine campaign control plane). Endpoints: `POST /api/v1/backfill-estimates`,
  `GET/POST /api/v1/backfill-campaigns` (`?limit=`), `GET /api/v1/backfill-campaigns/{id}`,
  `GET /api/v1/backfill-campaigns/{id}/events` (SSE), `POST /api/v1/backfill-campaigns/{id}/actions`,
  `GET /api/v1/control-plane/status`, `GET /api/v1/machines`. Backend:
  `backend/backfill_dashboard/control_plane/campaign_routes.py` + `status_routes.py`, `backend/README.md` ("All-machine campaign API").
  Old UI: `src/features/campaigns/*` (git history). Intended UX: an Admin route
  `/admin/campaigns` with estimate → review → queue, then a live campaign monitor (SSE deltas,
  pause/resume/cancel per campaign and machine). On Databricks the adapter refuses campaign
  submissions until dispatch is idempotent (runbook 4e).
- **Cost**. Endpoint: `GET /api/outerbounds/cost-report` (read-only JSON feed, see
  `backend/README.md`). Old UI: `src/components/current-cost/CurrentCostPanel.tsx`. Intended UX:
  a read-only Admin card with the current Outerbounds spend as text (no chart).
- **Log viewer**. Endpoint: `POST /api/admin/logs` (argo logs, local runtime only; logs are
  public on the GitHub Actions backend so it stays off there) and the `stdout`/`stderr` fields of
  `GET /api/admin/actions`. Old UI: `src/components/log-viewer/LogViewer.tsx` and the machine
  "Observability" tab in `src/App.tsx`. Intended UX: a collapsible, searchable stdout/stderr
  viewer in the Admin action details (local runtime), with error/warning highlighting.
- Not carried over either: the Sandbox command builder (`VITE_ENABLE_SANDBOX`, removed), the
  machine feature chart (`MachineFeatureChart`, charts are out of scope), the daily-gaps manifest
  form (`POST /api/admin/manifests/daily-gaps`), single/multi manifest forms
  (`/api/admin/manifests/machine`, `/multiple`), `argo-workflows create` and the all-machines
  legacy trigger (`POST /api/admin/backfills/all`; the new Backfill page sends the "all" scope
  through `/api/admin/backfills/orchestrated` with an explicit month plan, as the old runner did).

## v2 layout (spec v2)

- Shell: collapsible sidebar (256/64 px, persisted in `localStorage.ulrpm-sidebar`; off-canvas below
  1024 px) with MONITORING and ADMIN (amber) groups, nav badges read from the cached workflow
  summary (no extra request), and a footer with the active source and scan age. Breadcrumb top bar;
  amber ADMIN band on admin routes. Light/dark theme.
- Every page has a purpose line (`PageHeader`) and every section a one-sentence description (`Section`).
- Big `KpiCard`s (icon, value, helper, tooltip; loading/scanning/stale/error states; click to filter).
- `DataTable`: sort, page size 10/25/50/100 (default 25, admin tables 10), "Showing a–b of N",
  first/prev/numbers/next/last; page/size/sort live in the URL (per-table prefix).
- Fleet: debounced search, multi-status filter with counts, hide-test, clear-all, compact density, coverage meter.
- "(24h)" KPIs are computed in the browser: workflows by `finished_at` (fallback `started_at`/`created_at`),
  admin actions by `created_at` / `finished_at`; "Actions today" uses the Asia/Jerusalem calendar day.
- Not adopted from the spec: Tailwind/shadcn/TanStack (hand-written CSS tokens + a small table component instead).
