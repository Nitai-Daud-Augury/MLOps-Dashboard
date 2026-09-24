# FST Backfill Campaign Dashboard Backend

API for read-only coverage monitoring plus a durable, fail-closed all-machine
campaign control plane. Production dispatch is disabled unless inventory,
capacity, pricing, benchmarks, authorization, and persistence are verified.

## Run

```bash
cd drift-dashboard
npm run api
npm run dev
```

The frontend expects the API at `http://localhost:8000` by default. Override with:

```bash
VITE_BACKFILL_API_BASE_URL=http://host:port npm run dev
```

## Important Environment Variables

- `FST_PROD_ACCOUNT_NAME`, default `auguryprodfsthns`
- `FST_PROD_CONTAINER`, default `feature-store-container`
- `FST_PROD_ACCOUNT_KEY` or `FST_PROD_SAS_TOKEN`, optional; otherwise Azure default credentials are used
- `ULRPM_MACHINE_IDS_FILE`, defaults to the generated ULRPM machine list in `MLOps research`
- `AUGURY_MACHINE_URL_TEMPLATE`, default `https://app.augury.com/#/machine_health/machines/{machine_id}`
- `DATABRICKS_SILVER_ENABLED=1`, optional silver consistency scan
- `ULRPM_NO_DATA_BEFORE_YEAR` / `ULRPM_NO_DATA_BEFORE_MONTH`, defaults to `2025/05`; missing
  partitions before this cutoff are classified as expected no-source-data candidates
- Mongo inventory is disabled in this deployment. The dashboard reads only the
  curated 42-machine ULRPM inventory file specified by `ULRPM_MACHINE_IDS_FILE`.
- `OUTERBOUNDS_COST_REPORT_API_URL` and optional `OUTERBOUNDS_COST_REPORT_TOKEN`
  configure the read-only JSON feed for the Outerbounds **Current cost** tab.
  The browser history page itself is authenticated HTML, so its API endpoint
  must be supplied server-side rather than exposed to the frontend.
- `BACKFILL_ADMIN_PYTHON`, optional Python executable for Metaflow admin commands
- `BACKFILL_SCAN_IDLE_TIMEOUT_SECONDS`, default `600`; auto-cancel scans only after this
  many seconds without a heartbeat (not based on total scan runtime)
- `BACKFILL_SCAN_HEARTBEAT_SECONDS`, default `30`; refreshes scan liveness while a large
  Parquet range read is still active, without changing the completed-partition counters
- Production Parquet scans use PyArrow's native Azure random-access filesystem to fetch the
  footer and selected ultrasonic columns concurrently; the Azure SDK range reader remains a fallback
- `FST_BACKFILL_ACCOUNT_NAME`, default `aifleetmlopsfstbackfill`
- `FST_BACKFILL_CONTAINER_NAME`, default `fst-backfill`
- `FST_BACKFILL_ACCOUNT_KEY` or `FST_BACKFILL_SAS_TOKEN`, optional for uploading machine-scoped manifests
- `BACKFILL_QUEUED_SUBMISSION_TTL_SECONDS`, default `1800`; queued trigger actions older than
  this never entered the Argo command runner, so their machine/month reservations are failed
  and released on the next runner-status or machine-status request
- The deployed ULRPM parent orchestrator must report compatibility contract
  `2026-09-17-v4`; dashboard readiness rejects older templates.

The API never writes to Feature Store. Status manifest export is CSV; the admin machine
manifest action writes only to the dedicated backfill manifest storage.

The machine page's Feature graph uses
`GET /api/machines/{machine_id}/feature-series?year=YYYY&month=M&feature=ultrasonic_p2p`.
It reads the requested v1/v2 pair from the exact account, container, and monthly partition
recorded in the latest scan snapshot. The endpoint is read-only and caps chart responses at
5,000 sampled points.

Coverage uses the earliest ULRPM endpoint `installation_date` from Mongo as the authoritative
machine start. Months before installation are excluded from the denominator as `not_installed`;
when that lifecycle field is unavailable, the first observed `part-0.parquet` month remains the
fail-open fallback. Monthly `activity_status` is `online` when the partition has rows, `offline`
when it is missing or empty after installation, and `unknown` when the scan fails. Activity is
reported separately from backfill completeness. The source picker endpoints only list Azure
storage accounts and containers; they do not modify storage.

## Admin Endpoints

- `GET /api/admin/spec` returns known FSTBackfill params and defaults.
- `POST /api/admin/manifests/machine` uploads a one-machine parquet manifest with
  `machine_id`, `since`, and `until`; the returned `manifest_path` can be passed to trigger.
- `POST /api/admin/manifests/orchestrated` uploads the selected per-machine, per-month manifests
  used by the ULRPM parent without triggering a workflow. This powers the manifest-only action
  on both the machine Backfill page and Admin Actions → Backfill runner.
- `POST /api/admin/workflows/create` runs:
  `python FSTBackfill_prod_flow.py --no-pylint argo-workflows create`
- `POST /api/admin/workflows/trigger` runs:
  `python FSTBackfill_prod_flow.py --no-pylint --with retry argo-workflows trigger ...`
- `POST /api/admin/workflows/preflight` verifies the selected flow can start its CLI before a
  create or trigger request is accepted, avoiding failed queued actions when the local
  Metaflow environment is incompatible.
- `GET /api/admin/workflows/running` lists currently running Argo workflows that look like
  `FSTBackfill` runs, using `argo list -n jobs-default --running -o json`.
- `POST /api/admin/workflows/terminate` terminates the currently running FSTBackfill workflow
  by workflow id when provided, otherwise for a namespace using
  `metaflow-bx/bx_scripts/argo_handler.py`.
- `POST /api/admin/logs` runs:
  `python FSTBackfill_prod_flow.py logs <run>/<step>/<task> --stdout|--stderr`

The low-level `POST /api/admin/workflows/trigger` production action requires
`confirm_production=true` and confirmation text
`RUN_PROD_BACKFILL`.
Production stop requires `confirm_production=true` and confirmation text
`STOP_PROD_BACKFILL`.

The machine Backfill page and Admin Actions → Backfill runner both support dev/test and production
targets. Both choices submit the same deployed `UlrpmDevBackfillOrchestratorFlow` template and
therefore keep the same orchestrator branch. The target selector changes only the orchestrator's
`name_space` parameter: the configured dev namespace or `feature-store-container` for production.
The child flow derives `is_prod` from the selected namespace and only runs its existing dev
feature-seeding block when `not is_prod`. Selecting `feature-store-container` therefore skips
dev seeding without changing the dev execution path.

Outerbounds links use `flow_id=FSTBackfill`.

Orchestrated trigger reservations are reconciled with submission outcomes. A successful submit
replaces the temporary dashboard action ID with the Argo workflow ID; a failed submit releases
its reservations. Interrupted cleanup is persisted and retried, so a dashboard restart cannot
leave a never-started queued action permanently marking machines busy.

ULRPM submissions are also guarded by a dashboard/template compatibility contract. The dashboard
reads the deployed WorkflowTemplate before reserving months and rejects a stale or unversioned
parent. This prevents a dashboard calendar that includes a new UTC month from submitting that
month to an older packaged parent. The current contract is `2026-09-17-v4`; its month calendar is
evaluated at parent runtime and therefore advances without another source edit. Check the live
result under `GET /api/admin/readiness` → `argo`.

The v4 parent isolates execution failures by machine/month and checks both Metaflow metadata and
the authoritative Argo workflow phase. A failed child, a child that exceeds the bounded wait, or
a child-log validation error is written to the trace ledger and durable control record, then the lane
continues to its next month and the other machine lanes continue. `skipped_no_fe_output` remains
a successful terminal outcome. The parent finishes with `ORCHESTRATION_SUMMARY` and a best-effort
blob summary under `dashboard-control/batches/<batch_id>/summary.json`; inspect
`status=completed_with_errors` and retry only the listed failed months. Invalid configuration and
safety-contract violations still fail before any child is submitted.

## All-machine campaign API

- `GET /api/v1/machines` uses seek pagination over the synchronized read model.
- `POST /api/v1/backfill-estimates` persists an immutable signed estimate.
- `POST /api/v1/backfill-campaigns` queues only that exact estimate.
- `GET /api/v1/backfill-campaigns/{id}/events` emits coalesced SSE deltas.
- `GET /api/v1/capacity`, `/pricing`, `/runtime-benchmarks`, and
  `/control-plane/status` expose assumptions and production blockers.
- `POST /api/v1/runtime-benchmarks` records authenticated canary telemetry;
  campaign and machine action endpoints provide audited pause/resume/cancel.

The SQLite control plane uses WAL, atomic inventory swaps, unique work-item
idempotency keys, lease-safe dispatch, and one ready window per machine. Use a
persistent mounted `BACKFILL_CONTROL_PLANE_DB_PATH` in production. Keep
`BACKFILL_DISPATCH_ENABLED=0` until the canary gates in
`BACKFILL_CAMPAIGNS.md` have been completed.

Estimates show separate allocated-share and node-billing ranges. Scheduler and
retry factors are explicit assumptions; storage transaction cost stays marked
unknown until reliable usage and rates are available.
