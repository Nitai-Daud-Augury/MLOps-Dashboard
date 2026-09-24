# MLOps Dashboard

MLOps Dashboard is an internal monitor for curated ULRPM Feature Store (FST) coverage. It
compares monthly FST data and backfill status, joins machine lifecycle information from Mongo,
and makes offline, pre-install, and uncertain periods visible so they are not mistaken for
actionable gaps. A test-machine badge identifies machines whose inventory name/tags identify
them as test assets; the badge is informational and does not remove them from inventory.

## Data and coverage rules

- `Online`: the machine is installed and the scanned FST month contains rows. Only online months
  count in the expected-coverage denominator. `backfilled` online months count as complete;
  `needs_backfill` online months count as gaps.
- `Offline`: an installed-period month has no FST data. It is excluded from coverage and cannot
  be selected for backfill.
- `Not installed`: before the lifecycle installation date; excluded from expected coverage.
- `Unknown` or scan error: the scan could not establish activity. It is not backfillable.
- Mongo lifecycle lookup is fail-open: if it is unavailable, the scan still runs and surfaces a
  warning. Treat installation dates as unverified until lifecycle status is healthy.

The scanner uses the checked-in curated cohort at `data/unique_machine_ids.txt`; Mongo does not
expand that FST scan cohort. Inventory/runner APIs may separately use the configured inventory
provider. Review cohort changes rather than broadening the file by default.

## Architecture and runtime limits

The React/Vite UI and FastAPI API share one origin in Docker and Databricks Apps. FastAPI owns
`/api/*`, serves built assets under `/assets/*`, and sends non-API GET deep links to the React
entry point. Unknown API paths remain JSON 404s.

| Runtime | Monitor / scan | Workflow mutations | Notes |
|---|---:|---:|---|
| Local | Yes, with configured FST access | Capability enabled, still subject to readiness/auth checks | Vite + uvicorn are separate processes. |
| Docker | Yes, with configured FST access | Disabled | Safe monitor-only defaults; state under `/tmp` is ephemeral. |
| Databricks App | Yes, once external FST credentials/network are configured | Disabled server-side and in UI | Mongo uses a Databricks secret resource; workflow triggers still require a future remote adapter. |

Manifest generation is separate from workflow triggering and requires its own storage write
credential. The current bundle does not configure FST read or manifest-write credentials. Do not
assume deployed scans or manifest uploads are ready until those resources/network paths have been
provisioned and verified. The campaign SQLite/JSON state uses ephemeral app storage in the current
package and is not a durable production dispatch control plane.

## Repository layout

- `src/`: React UI, dashboard and machine views, backfill controls, API types.
- `backend/backfill_dashboard/`: FastAPI routes, scanner, lifecycle and coverage normalization,
  storage providers, and control plane.
- `backend/tests/`: backend and packaging regression tests.
- `data/unique_machine_ids.txt`: curated scan cohort.
- `app.yaml`, `databricks.yml`, `resources/`: Databricks App and bundle configuration.
- `.github/workflows/`: PR/main CI, Databricks deploy, and manual app lifecycle controls.
- `Dockerfile`: multi-stage local/container build.

## Prerequisites

- Node.js 22+ and npm.
- Python 3.11 (a project virtual environment is recommended).
- Read access to the intended FST storage source and, for installation dates, the approved Mongo
  lifecycle database or Azure Key Vault source.
- For deployment only: Databricks CLI with caller-managed authentication, GitHub OIDC federation,
  and the environment variables listed in the [deployment runbook](docs/DEPLOYMENT_RUNBOOK.md).

## Local setup and development

From the repository root:

```bash
cp .env.example .env
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
npm ci
```

Edit `.env` locally with the approved FST source and credentials. For accurate installation
boundaries, also configure Mongo (`MONGODB_URL`) or intentionally enable local Azure Key Vault
lookup. Do not commit `.env`.

Start two terminals from the repository root, activating `.venv` in each:

```bash
# Terminal 1 — FastAPI on 127.0.0.1:8000
npm run api
```

```bash
# Terminal 2 — Vite on its local development port
npm run dev
```

Open the address printed by Vite. Its `/api` proxy targets the local FastAPI process. Stop each
process with `Ctrl-C` in its terminal.

Useful checks:

```bash
npm run lint
npm run build
```

```bash
cd backend
python -m pip install pytest
python -m pytest -q tests
```

## Docker

Build and run a monitor-only local image:

```bash
docker build -t mlops-dashboard:local .
docker run --rm --name mlops-dashboard -p 8000:8000 \
  -e FST_PROD_ACCOUNT_NAME \
  -e FST_PROD_CONTAINER \
  -e FST_PROD_ACCOUNT_KEY \
  mlops-dashboard:local
```

Load the FST account, container, and key into those shell environment variables through your
approved local secret mechanism before running the command. Environment-variable inheritance
avoids putting secret values in command history. Do not use `--env-file .env` here: the local
example sets the local runtime mode and could override the container's monitor-only default.
Check `http://127.0.0.1:8000/api/health`, then visit the root or a machine deep link. Stop with
`Ctrl-C`, or use `docker stop mlops-dashboard` from another terminal. The container stores
runtime state under `/tmp`; it is disposable and not appropriate for durable workflow dispatch.

## Deploy and operate

The repository includes dev/prod Databricks Bundle targets and three GitHub workflows. Pull
requests run CI only. A successful push to `main` verifies and deploys the dev app, starts it for
readiness, then stops it by default. Production deployment and all lifecycle controls are manual;
prod uses a protected GitHub Environment. An explicit manual keep-running selection is the only
workflow path that leaves a successfully verified app running.

The app is monitor-only in Docker and Databricks. Local Argo/Metaflow triggers, stop/log/requeue
operations, and campaign dispatch are not supported in deployed runtimes. See
[docs/DEPLOYMENT_RUNBOOK.md](docs/DEPLOYMENT_RUNBOOK.md) for one-time GitHub/Databricks setup,
secret binding, commands, verification, cost controls, rollback, and troubleshooting.
