# MLOps Dashboard deployment and operator runbook

This runbook describes the repository configuration as implemented. Repository changes do not
create the GitHub repository, Databricks service principal/federation policy, secret scope, app, or
cloud storage grants. Those one-time owner actions remain pending. Do not authenticate, deploy, or
start a shared app until those owners approve the target workspace and costs.

## Current deployment contract

- App names are `mlops-dashboard-dev` and `mlops-dashboard` (prod).
- The app is monitor-only in Docker and Databricks. Local Argo/Metaflow mutation APIs return 403
  in deployed modes; UI controls are disabled too. Scanning reads FST; Mongo adds lifecycle data.
- `BACKFILL_DISPATCH_ENABLED=0` and `BACKFILL_PRODUCTION_MODE=0` are set in the Databricks app
  manifest. No remote workflow trigger adapter is implemented.
- The Bundle app resource is `mlops_dashboard`. It binds one Databricks secret resource for
  `MONGODB_URL`, with `READ` scope permission. FST read credentials, manifest-write credentials,
  Azure network access, and an optional SQL warehouse are not currently bound by the Bundle.
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

### 4. Provision FST access and network reachability

This is a required external setup item before expecting a Databricks deployment to complete an
FST scan. The current Bundle binds Mongo only. The deployment owner must separately choose and
provision a least-privilege FST read credential/identity that Databricks Apps can use to reach the
approved Azure Blob account/container, and confirm workspace network egress. No FST credential is
checked in or currently mapped as a Bundle resource. Manifest upload needs a separate write
credential and is not required for monitor-only operation. Silver comparison is disabled by
default and has no warehouse resource binding in the Bundle.

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
   errors (the snapshot does not expose a separate `lifecycle_status` field).
5. Confirm the expected FST account/container, curated cohort, and scanned dates. Verify sample
   known machines against Augury installation metadata: pre-install months are `not_installed`,
   data-bearing months are `online`, and no-data installed months are `offline`.
6. Confirm the machine monthly totals count only online months. Offline, pre-install, and unknown
   months are excluded from coverage and disabled for backfill selection. Test-named/tagged
   machines carry the `Test` badge.
7. Confirm Mongo lifecycle status is healthy and installation dates are present. Until this
   succeeds, the scanner deliberately continues fail-open and installation boundaries may be
   unknown; do not sign off on those dates.
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
| `snapshot.warnings` says lifecycle unavailable | Verify Mongo secret access and connectivity. The scan continues fail-open, but installation-based classification is not authoritative until Mongo becomes healthy and a new scan completes. |
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
