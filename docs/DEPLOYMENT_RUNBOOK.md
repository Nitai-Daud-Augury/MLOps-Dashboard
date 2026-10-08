# MLOps Dashboard deployment and operator runbook

This runbook describes the repository configuration as implemented. Repository changes do not
create the GitHub repository, Databricks service principal/federation policy, secret scope, app, or
cloud storage grants. Those one-time owner actions remain pending. Do not authenticate, deploy, or
start a shared app until those owners approve the target workspace and costs.

## Current deployment contract

- App names are `mlops-dashboard-dev` and `mlops-dashboard` (prod).
- The app is monitor-only in Docker and Databricks. Local Argo/Metaflow mutation APIs return 403
  in deployed modes; UI controls are disabled too. Scanning reads FST; lifecycle enrichment is
  selected by `LIFECYCLE_SOURCE` (`databricks` in the deployed app manifest, `mongo` by default
  locally when unset). Databricks uses bronze `dih_prod.bronze_augury_mh_mongodb.machines_raw`
  (latest `ingestion_timestamp` per `_id`) via the bound SQL warehouse. Lookup remains fail-open.
- `BACKFILL_DISPATCH_ENABLED=0` and `BACKFILL_PRODUCTION_MODE=0` are set in the Databricks app
  manifest. No remote workflow trigger adapter is implemented.
- The Bundle app resource is `mlops_dashboard`. It binds Databricks secret resources for
  `MONGODB_URL` and `FST_PROD_ACCOUNT_KEY` (`READ`), plus SQL warehouse `sql-warehouse`
  (`6ed9ddd0b2661edc`, `CAN_USE`) injected as `DATABRICKS_WAREHOUSE_ID`. Manifest-write
  credentials and broader Azure network grants remain external owner actions.
- The Bundle derives its workspace root from the authenticated caller. For prod, deployments must
  run as the approved deployment service principal; no user, workspace host, or principal is
  hardcoded in the repository.
- App state and campaign/control-plane JSON/SQLite files use ephemeral local storage in the current
  package. Do not enable workflow dispatch or treat these files as durable production state.

## One-time GitHub and Databricks setup

### 1. Create and protect the source repository

1. Create a private repository in the approved Augury GitHub organization, using this project as
   its root. Git initialization/push must be done by the repository owner; it has not been done by
   this implementation.
2. Protect `main`: require pull requests, at least one review, and the `Dashboard CI` check before
   merge. Disallow force pushes and branch deletion.
3. Create GitHub Environments named exactly `databricks-dev` and `databricks-prod`.
4. Add the four required **Environment variables** to both environments (not GitHub secrets):

   | Variable | Value |
   |---|---|
   | `DATABRICKS_HOST` | Workspace host URL for that environment. |
   | `DATABRICKS_CLIENT_ID` | Databricks service principal application/client ID. |
   | `MONGODB_SECRET_SCOPE` | Existing Databricks secret scope name for that app/workspace. |
   | `MONGODB_SECRET_KEY` | Secret key name containing the Mongo connection string. |

   These variables contain resource identifiers, not Mongo credentials. Never create a GitHub
   variable or workflow input for the Mongo URI itself.
5. Add required reviewers and prevent self-review on `databricks-prod`. Restrict allowed deployment
   branches to `main` (or the stricter approved release branch policy). The workflow also refuses
   prod dispatches from any ref except `main`.

See [GitHub deployment environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)
for approval protection and environment variable setup.

### 2. Configure Databricks OIDC federation and least privilege

Create or designate separate dev/prod service principals according to the workspace security
model. For each environment, create a Databricks service-principal federation policy for the
private repository and the matching GitHub Environment subject. Use the GitHub issuer
`https://token.actions.githubusercontent.com`, the organization-approved audience, and a subject
scoped to the exact repository and environment (for example, the `databricks-dev` or
`databricks-prod` environment). Do not grant a wildcard repository/branch subject.

Grant only the workspace permissions required to validate/deploy the Bundle and manage the named
app and its bundle workspace root. The app service principal (distinct from the deployment
principal where applicable) needs read access to the Mongo secret scope and whatever approved
Azure/FST data-plane identity or credentials are provisioned. It needs no Argo/Metaflow workflow
permissions because deployed workflow mutations are intentionally unavailable.

GitHub Actions authenticates with OIDC using `DATABRICKS_AUTH_TYPE=github-oidc`,
`DATABRICKS_HOST`, and `DATABRICKS_CLIENT_ID`; no Databricks PAT/client secret belongs in GitHub.
The current workflows pin `databricks/setup-cli` to the tested/documented `v1.10.0` release tag.

Official setup references:

- [GitHub Actions OIDC federation](https://docs.databricks.com/aws/en/dev-tools/auth/provider-github)
- [Service-principal federation policies](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-federation-policy)
- [Databricks Apps GitHub Actions CI/CD](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/cicd-github-actions)
- [Databricks CLI setup action](https://github.com/databricks/setup-cli)

### 3. Provision the Mongo secret resource

Use a dedicated secret scope for the dashboard so its app resource's `READ` permission does not
grant access to unrelated secrets. Add a key whose name matches `MONGODB_SECRET_KEY`; put the
approved Mongo connection string in that key using the workspace secret UI or a secure interactive
CLI flow. Never place the connection string in `app.yaml`, `databricks.yml`, a GitHub variable,
workflow input, command-line argument, or log. Scope-level app permission means the app can read
all keys in that scope, so keep it narrowly scoped.

The app manifest maps the secret resource key `mongodb_url` to `MONGODB_URL` with `valueFrom`.
The bundle variables choose the scope and key names per target. Databricks secret resources keep
the URI out of the checked-in config; see [Apps resources](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/resources)
and [App environment variables](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/environment-variables).

For local development only, a developer may put `MONGODB_URL` in their ignored `.env`, or enable
the configured Azure Key Vault lookup using a valid local Azure identity. Do not copy a secret
value into this runbook or a shell command.

### 4a. Lifecycle source (Databricks bronze machines_raw vs Mongo)

Deployed `app.yaml` sets `LIFECYCLE_SOURCE=databricks` and injects `DATABRICKS_WAREHOUSE_ID`
from the Bundle `sql-warehouse` resource. Optional `DATABRICKS_MACHINES_RAW_TABLE` defaults to
`dih_prod.bronze_augury_mh_mongodb.machines_raw` (legacy `DATABRICKS_EQUIPMENT_TABLE` is still
accepted as an override). The table is append-only: lookups take the latest row per machine
`_id` by `ingestion_timestamp`.

**Auth.** Inside Databricks Apps (`MLOPS_DASHBOARD_RUNTIME=databricks`) SQL connections use
`databricks.sdk.core.Config()` with no profile, i.e. the app service principal from the
injected `DATABRICKS_HOST` / `DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` (OAuth M2M);
no Databricks CLI or `DATABRICKS_PROFILE` is needed or used there. Locally a profile is used
only when `DATABRICKS_PROFILE` is set (e.g. `mlops-dev`, see `.env.example`); otherwise the SDK
default auth chain applies. All statements of one provider share one `Config` (one token
cache) and token acquisition is serialized, so the parallel cross-check queries cannot race a
token refresh. The app service principal needs `CAN_USE` on the bound warehouse plus
`USE CATALOG dih_prod`, `USE SCHEMA` and `SELECT` on every table it reads (machines_raw; and
both feature_store tables if `FEATURES_CROSSCHECK=feature_store`).

**Semantic shift:** Mongo lifecycle used endpoint `installation_date`. The Databricks provider
maps bronze `raw_json.created_at` to `installation_at`. `firstRecorded.timestamp` is a go-live /
first-sensor proxy and is used only when `created_at` is missing. The retired Silver ontology
`createdAt` / `isInstalled` path is no longer used.

**Test machines:** there is no bronze `is_test` flag. A machine is marked test when its display
name or tags match the existing word heuristic (`test` / `e2e` / `qa`), when company name is one
of `QA`, `Demo`, `Hagay test lab`, or `dynamicsScopingTest`, or when its id is listed in
`data/test_machine_ids.txt` / `MLOPS_TEST_MACHINE_IDS` (scanner combines these).

`openapi` is reserved and currently returns `not_configured` (OAuth not ready). `off` disables
lifecycle enrichment. Failures (`unreachable` / `unauthorized` / `not_configured`) warn and the
scan continues fail-open, same as the Mongo path.

**Machine list (membership) is file-based.** The scan cohort is exactly the IDs in
`ULRPM_MACHINE_IDS_FILE` (default `data/unique_machine_ids.txt`, the curated 42-machine ULRPM
list). `LIFECYCLE_SOURCE` is the single switch for lifecycle *enrichment* only (display name,
test flag, installation / first-recorded / cutoff dates); neither Databricks nor Mongo adds or
removes machines. There is no `DATA_ENV` variable.

**One data-start rule for every source.** Both providers carry `firstRecorded.timestamp`
(`first_recorded_at`). Per machine the scanner uses:

- *data start* = earliest of the first-recorded month and the first FST partition month. A
  missing month before it is `no_source_data`, never `needs_backfill` (e.g. a machine created
  2025-08-28 whose first sample is 2025-09-03 is not flagged for 2025-08).
- *installation boundary* = the source's installation date, clamped to the data start. A month
  that has an FST partition is always read, even if it predates the reported installation date
  (Mongo endpoint `installation_date` reflects the current sensors and can be later than the
  first real data after a sensor swap). Only months with no partition before the boundary are
  `not_installed`.

With this rule Mongo and Databricks produce the same backfill window and gaps for the same
FST data.

**Which source is active.** `GET /api/backfill/status` includes a read-only `data_sources`
block (`lifecycle_source`, `lifecycle_status`, `lifecycle_table` / `lifecycle_warehouse_id` /
`databricks_auth` / `databricks_profile` for Databricks, `inventory_source`, `machine_count`), and the API logs one
`data_sources: ...` line at startup. Each scan's first warning also names the source. Locally:

```bash
curl -s localhost:8000/api/backfill/status | python3 -m json.tool | grep -A8 data_sources
```

To switch locally, set `LIFECYCLE_SOURCE=mongo` or `databricks` in `.env` and restart the API
(`npm run api`), then start a new scan.

### 4b. Optional Feature Store cross-check (`FEATURES_CROSSCHECK`)

Azure Blob (FST Parquet partitions) is the **only** source of truth for scan
statuses. `FEATURES_CROSSCHECK=feature_store` (default off/empty) adds an
informational comparison against Databricks `dih_prod.silver_mh.feature_store`
(override with `DATABRICKS_FEATURE_STORE_TABLE`). It never changes a month or
machine status.

- **Query:** one aggregate SQL per scan on the same profile/warehouse and the
  same timeouts + cold-warehouse retry as `LIFECYCLE_SOURCE=databricks`
  (`machine_id IN (<cohort>)`, `recorded_at` within the scan window, grouped
  per machine/month: `COUNT(*)` plus non-null counts of the 8 v2 ultrasonic
  target features and their v1 counterparts). It runs alongside the Parquet scan;
  warm cost is ~20 s for the 42-machine cohort.
- **Staleness gate:** a second query, run concurrently, reads the bronze mirror
  `dih_prod.bronze_augury_mh_blob.feature_store` (override with
  `DATABRICKS_FEATURE_STORE_BRONZE_TABLE`): latest `_file_modification_time` per
  `_source_file` for the cohort (`machine_id IN (...)`), mapped back to
  machine/month from the `machine_id=/quarter=/month=/partition_version=last/part-0.parquet`
  path. The bronze job only re-merges the **current and previous month** nightly,
  so blob files rewritten later (backfilled older months) are never re-ingested.
  When the blob file's `last_modified` (already read by the scan) is newer than
  bronze's copy, or bronze has no copy, the month becomes
  `crosscheck.status = "feature_store_stale"` with `blob_modified`,
  `bronze_modified`, `stale_reason` and `suppressed_flags` (the row/v2
  differences that were not judged). Real mismatches are flagged only when
  Databricks holds the current file version. If the staleness query fails, the
  plain comparison is kept and the scan warning says staleness could not be checked.
- **Flags per machine/month** (`crosscheck.flags`):
  - `row_ratio`: `blob_rows / feature_store_rows` outside `[1 - tol, 1 + tol]`,
    `tol = FEATURES_CROSSCHECK_ROW_TOLERANCE` (default `0.10`).
  - `v2_presence`: a v2 target feature has data on one side only.
  - `missing_in_feature_store` / `missing_in_blob`: the month has rows on one side only.
  - `feature_store_stale`: Databricks copy is older than blob (see staleness gate).
- **Not judged:** schema completeness. Against the canonical FST contract the
  Databricks tables have **23 truly absent columns** (`fullbw_/lowbw_vib_vel_order*_prod`
  (18), `edge_mag_on_detect_decision`, `edge_mag_on_detect_p2p`,
  `endpoint_relocation_p_value_thresh`, `endpoint_relocation_timestamp_thresh`,
  `machine_non_stationary_score`) **plus 11 case-duplicate column pairs collapsed
  by Databricks** (e.g. `magnetic_magLF` / `magnetic_maglf`; Databricks column names
  are case-insensitive). Full-schema completeness (`missing_schema_columns`) stays
  blob-only and is never compared or flagged.
- **Fail-open:** any error/timeout (`FEATURES_CROSSCHECK_TIMEOUT_SECONDS`,
  default 300) sets `snapshot.crosscheck.status = "error"` and adds a scan
  warning; statuses are unaffected.
- **Where to see it:**
  - `GET /api/backfill/crosscheck` - summary, `counts` (real mismatches vs
    stale vs clean, staleness status), `flagged` (real mismatches) and `stale`.
  - `GET /api/backfill/status` - `snapshot.crosscheck` (run summary),
    `machines[].crosscheck`, `machines[].months[].crosscheck`, and
    `data_sources.features_crosscheck` (enabled, table, bronze_table, tolerance,
    last_run incl. stale/real counts and staleness_status).
  - UI month grid: a small purple `≠` marker on real mismatches and a grey `⏱`
    marker on stale months that would have differed ("Databricks copy is older
    than blob"); hover for numbers and timestamps. Legend entries appear only when used.
  - Startup log line: `data_sources: ... features_crosscheck=feature_store feature_store_table=... feature_store_bronze_table=... crosscheck_staleness_gate=on crosscheck_row_tolerance=...`.
- **Turn on locally:** set `FEATURES_CROSSCHECK=feature_store` in `.env`,
  restart `npm run api`, start a rescan, then:

  ```bash
  curl -s localhost:8000/api/backfill/crosscheck | python3 -m json.tool | head -60
  curl -s localhost:8000/api/backfill/status | python3 -m json.tool | grep -A12 features_crosscheck
  ```

The Silver consistency check (`DATABRICKS_SILVER_ENABLED=1`) now defaults to
`dih_prod.silver_mh.feature_store`; `dih_prod.silver_mh.features` is deprecated
and stale.

### 4c. Which data source fed a scan (`[data-source]` logs)

Every data-source decision is logged with the prefix `[data-source]` on the
logger `uvicorn.error.data_source`, so it appears in the `npm run api` terminal
at INFO without extra logging config. Lines: startup (lifecycle configured vs
provider actually used, inventory file + machine count, blob account/container,
crosscheck on/off + tables), each Databricks SQL attempt (warm/cold/retry,
budget, elapsed), each lifecycle fetch (requested/returned/enriched, duration),
and one `scan done:` summary per scan. Fallbacks and fail-open paths
(provider unavailable, SQL timeout/auth error, crosscheck or staleness failure,
Mongo credential/ping failure) are WARNING lines that say what the scan fell
back to. There is no Databricks-to-Mongo fallback. Errors are redacted (no
connection strings or tokens).

```bash
npm run api 2>&1 | tee /tmp/api.log
grep '\[data-source\]' /tmp/api.log
```

Each machine in `/api/backfill/status` also carries `lifecycle_source`
(`databricks`/`mongo`/`null`) and `lifecycle_enriched`, shown as the hover
title on the machine name in the coverage table.

## 4. Provision FST access and network reachability

This is a required external setup item before expecting a Databricks deployment to complete an
FST scan. The current Bundle binds Mongo only. The deployment owner must separately choose and
provision a least-privilege FST read credential/identity that Databricks Apps can use to reach the
approved Azure Blob account/container, and confirm workspace network egress. FST account key is
mapped as a Bundle secret resource when the scope contains `fst_prod_account_key`. Manifest
upload needs a separate write credential and is not required for monitor-only operation.
Silver feature comparison (`DATABRICKS_SILVER_ENABLED`) remains disabled by default; the same
SQL warehouse resource now also serves `LIFECYCLE_SOURCE=databricks` machines_raw lookups.

## Bundle commands for a trusted local operator

Use an installed Databricks CLI version that supports Apps Bundle resources (the implementation
was schema-checked with CLI v1.10.0). Authenticate using your approved caller profile/environment;
the repository does not set or switch profiles. Select a real, pre-created secret scope/key name
locally as bundle variables; these are identifiers only, not the URI:

```bash
export BUNDLE_VAR_mongodb_secret_scope='<approved-secret-scope>'
export BUNDLE_VAR_mongodb_secret_key='<approved-secret-key>'

databricks bundle validate --target dev
databricks bundle deploy --target dev
databricks bundle run mlops_dashboard --target dev
```

The default bundle resource lifecycle keeps the app stopped during `bundle deploy`; `bundle run`
explicitly applies/starts the app. Use prod only after approved manual release controls are in
place and from `main`:

```bash
databricks bundle validate --target prod
databricks bundle deploy --target prod
databricks bundle run mlops_dashboard --target prod
```

These commands are examples only; running them contacts a workspace and can create/update
resources. They were not run as part of this implementation. `bundle destroy` deletes managed
resources; it is not a stop command.

## Workflow behavior

| Workflow | Trigger | Effect |
|---|---|---|
| `ci.yml` | Every pull request; pushes to `main` | Node lint/build and full backend tests only. No OIDC or deploy. |
| `deploy.yml` | Push to `main`; manual dispatch for `dev` or `prod` | Re-runs lint/build/backend checks first; validates and deploys selected bundle target; runs `mlops_dashboard`; polls app state until `RUNNING` or failure. Pushes target dev. Prod is manual, requires main, and uses `databricks-prod`. |
| `app-control.yml` | Manual dispatch only | Fixed dev/prod app mapping; `status` only reads, `start` and `stop` are explicit. Prod uses `databricks-prod`. |

Deploy stops the app after the readiness check by default. A manual `keep_running` selection may
leave the app running only after a successful readiness check; any failed run still attempts to
stop it. Deploy/control use serialized, non-canceling concurrency to avoid interrupting an app
lifecycle operation. The deployment workflow checks `app_status.state`; it does not bypass
Databricks authentication to probe a protected HTTP endpoint from GitHub.

## Preferred lifecycle operations

Use GitHub Actions → `MLOps Dashboard app control` → Run workflow. Choose `dev` or `prod`, then
choose `status`, `start`, or `stop`. The prod Environment approval policy applies before the job
can reach Databricks. Start incurs app compute; stop when finished. Status is read-only.

CLI fallback, from a trusted machine already authenticated to the correct workspace:

```bash
# Development
databricks apps get mlops-dashboard-dev --output json
databricks apps start mlops-dashboard-dev
databricks apps logs mlops-dashboard-dev --tail-lines 200
databricks apps stop mlops-dashboard-dev
databricks apps get mlops-dashboard-dev --output json

# Production — use only with production approval and access
databricks apps get mlops-dashboard --output json
databricks apps start mlops-dashboard
databricks apps logs mlops-dashboard --tail-lines 200
databricks apps stop mlops-dashboard
databricks apps get mlops-dashboard --output json
```

`databricks apps start` and `stop` wait for the lifecycle transition unless `--no-wait` is used.
`databricks apps logs <name> --follow` streams logs; interrupt streaming with `Ctrl-C`.
Commands and option forms were checked against installed Databricks CLI help.

## Cost controls

- Leave apps stopped when not in active use. Databricks bills Apps compute while the app is
  running; a stopped app is not accessible. The main-branch deploy workflow stops the dev app by
  default. Only a manual successful deployment with `keep_running=true` intentionally leaves it
  up. See [Apps overview and pricing behavior](https://docs.databricks.com/aws/en/dev-tools/databricks-apps).
- Stopping the app does not stop a SQL warehouse. Warehouses have independent auto-stop and
  compute charges; keep Silver comparison disabled until needed and ask the warehouse owner to
  set an approved short auto-stop. See [SQL warehouse settings](https://docs.databricks.com/aws/en/compute/sql-warehouse/create).
- Do not use `databricks bundle destroy` for routine cost control. It deletes resources rather
  than stopping compute.
- The current app's `/tmp` SQLite/JSON state is disposable. Do not persist or schedule backfill
  campaigns against it. A durable control-plane store is a separate future requirement.

## Post-deployment verification

After app start, get its URL and state in the workspace app page or with `databricks apps get`.
Check logs for startup errors, missing resource variables, Azure credential/network failures, or
Mongo connection errors. Then verify:

1. `GET /api/health` returns `ok: true` and runtime capabilities with `monitor: true` and
   `workflow_mutations: false`.
2. `/` and a React deep link such as `/machine/<machine-id>` both render the app.
3. An unknown path such as `/api/not-a-route` returns JSON 404, not SPA HTML.
4. `POST /api/backfill/scan` starts a read-only scan; poll `GET /api/backfill/status` until
   `scan_state.running` is false. Check `scan_state.error` and `snapshot.warnings` for lifecycle
   errors, and the top-level `data_sources` block for the process's `lifecycle_source` /
   `lifecycle_status` and inventory file.
5. Confirm the expected FST account/container, curated cohort, and scanned dates. Verify sample
   known machines against Augury installation metadata: months with an FST partition are
   always read (`online`), missing months before the reported installation are `not_installed`,
   missing months before the first recorded data are `no_source_data`, and later no-data months
   are `offline`.
6. Confirm the machine monthly totals count only online months. Offline, pre-install, and unknown
   months are excluded from coverage and disabled for backfill selection. Test-named/tagged
   machines carry the `Test` badge.
7. Confirm lifecycle status is healthy for the configured `LIFECYCLE_SOURCE` and that
   installation timestamps are present where expected. For `databricks`, remember these are
   bronze `created_at` values (not Mongo endpoint `installation_date`). Until lifecycle is healthy,
   the scanner continues fail-open and installation boundaries may be unknown; do not sign
   off on those dates.
8. Confirm workflow capability is disabled in `/api/admin/spec` and UI. No Argo/Metaflow trigger,
   cancel, stop, or requeue is supported in deployed mode.

Example read-only requests (run only against the chosen deployment):

```bash
curl -fsS '<app-url>/api/health'
curl -i '<app-url>/machine/<machine-id>'
curl -i '<app-url>/api/not-a-route'
curl -fsS -X POST '<app-url>/api/backfill/scan' -H 'Content-Type: application/json' -d '{}'
curl -fsS '<app-url>/api/backfill/status'
curl -fsS '<app-url>/api/admin/spec'
```

Protect the app URL if SSO requires an authenticated browser/session; never add access tokens to
these examples or logs. Live Databricks/FST/Mongo verification is pending external setup and was
not performed as part of this code change.

## Troubleshooting

| Symptom | Checks and safe next action |
|---|---|
| App never reaches `RUNNING` / crashes | Inspect `databricks apps logs <app-name> --tail-lines 200`; confirm `app.yaml` command, root `package.json` build, Python deps, the Mongo resource key, and app port. Retry only after fixing the reported startup issue. |
| OIDC fails in GitHub | Check exact `DATABRICKS_HOST` and service principal client ID environment variables, `id-token: write`, workflow Environment name, issuer/audience, repository/environment subject, and service principal workspace rights. No PAT fallback is configured. |
| Bundle says variable missing | Confirm `MONGODB_SECRET_SCOPE` and `MONGODB_SECRET_KEY` are set on the selected GitHub Environment; local bundle operators need `BUNDLE_VAR_mongodb_secret_scope` and `BUNDLE_VAR_mongodb_secret_key`. Values are names, not Mongo credentials. |
| Lifecycle status is `unauthorized`, unavailable, or expired | Check the secret scope/key, resource binding, secret rotation/expiry, Mongo user grants, and app network path. Refresh/rotate Mongo auth only when status reports unauthorized/unavailable/expired, or when the deployed secret was rotated. Update the secret securely and restart/redeploy the app so its resource-backed environment is refreshed. A healthy status does not require refreshing Mongo auth. |
| Mongo is not configured | The DB URI must be set in the dedicated Databricks secret resource (`MONGODB_URL`) or local ignored `.env`; do not add it to GitHub variables or command arguments. The scanner remains fail-open, so check installation evidence before treating missing months as real gaps. |
| FST scan returns auth/403/network errors | The bundle does not currently bind FST credentials. Ask the cloud/data owner to verify the account/container, app identity or read-only credential, Azure RBAC/SAS expiry, private endpoint/firewall rules, DNS, and outbound network policy. Do not broaden storage discovery or scan cohort as a workaround. |
| `snapshot.warnings` says lifecycle unavailable | Verify the active `LIFECYCLE_SOURCE`: for `databricks`, warehouse id/permissions and UC table access; for `mongo`, secret access and connectivity. The scan continues fail-open, but installation-based classification is not authoritative until lifecycle is healthy and a new scan completes. |
| `/api/health` works but no scan snapshot exists | Health confirms process readiness, not Azure/Mongo data access. Configure FST source credentials/network, start a scan, then inspect `scan_state.error` and warnings. |
| UI still reports stale machine coverage | Start a new scan after fixing source/lifecycle access. Persisted snapshots normalize coverage on read, but installation/date corrections need fresh source metadata. |
| Data exists but a month is disabled | Verify activity classification and row count in the machine/month detail. Only positively online data months are selectable; offline/not-installed/unknown fail closed by design. |
| GitHub deploy fails while app is already stopped | `bundle deploy` does not start the app. Read the action result; the workflow explicitly runs the resource afterward. If start/readiness failed, cleanup attempts a stop. |

## Rollback and release ownership

1. Pause further deployments if a release is unhealthy; use the production Environment approval
   gate for any prod action.
2. Identify the last known-good commit from the protected release history. Revert the faulty
   change through a reviewed pull request; do not force-push `main`.
3. Merge the revert after CI passes. The main workflow updates dev and stops it by default.
4. Verify dev health, lifecycle/Mongo status, deep links and monitor-only capability before any
   manual prod deployment. Use the prod approval gate and keep-running unchecked unless explicitly
   needed.
5. If only runtime compute is the concern, use app-control `stop`; do not destroy Bundle resources.

Before first deployment, name an owner for the private repository, Databricks dev/prod service
principals and federation policies, FST identity/network, Mongo scope/secret rotation, production
approval, and app/warehouse cost monitoring. Release checklist:

- [ ] Required PR review and CI checks are protected on `main`.
- [ ] `databricks-dev` and `databricks-prod` Environments contain the four required variables;
      prod requires independent approval.
- [ ] OIDC federation is restricted to this repository and the two environment subjects.
- [ ] Dedicated Mongo scope/key is present and app resource has read-only scope permission.
- [ ] FST Azure read access and network egress are provisioned and live scan verified.
- [ ] App state reports `RUNNING` during use and is `STOPPED` afterward.
- [ ] Runtime API reports monitor-only capabilities; no workflow mutation was enabled.
- [ ] Mongo secret rotation procedure and named operator owner are known.

## Not yet supported

The current deployed runtime has no remote Argo/Metaflow trigger, stop, logs, cancel, requeue, or
campaign execution adapter. It cannot safely orchestrate production backfills. The campaign
control-plane SQLite/JSON files are not durable in Databricks Apps. FST and manifest storage
resources also need deployment-owner provisioning. These gaps require separate reviewed changes;
do not remove runtime guards or enable dispatch flags to bypass them.
