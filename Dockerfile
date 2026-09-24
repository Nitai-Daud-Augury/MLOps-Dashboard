FROM node:22-alpine AS frontend-build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html vite.config.ts tsconfig.json tsconfig.app.json tsconfig.node.json ./
COPY src ./src
COPY public ./public
RUN npm run build

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    MLOPS_DASHBOARD_RUNTIME=docker \
    BACKFILL_DISPATCH_ENABLED=0 \
    BACKFILL_PRODUCTION_MODE=0 \
    MONGODB_KEY_VAULT_ENABLED=0 \
    BACKFILL_DASHBOARD_STATIC_DIR=/app/dist \
    BACKFILL_DASHBOARD_STATE_PATH=/tmp/ulrpm_backfill_dashboard_state.json \
    BACKFILL_CONTROL_PLANE_DB_PATH=/tmp/backfill_control_plane.sqlite3 \
    ULRPM_MACHINE_IDS_FILE=/app/data/unique_machine_ids.txt

WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY backend ./backend
COPY data ./data
COPY run.py ./run.py
COPY --from=frontend-build /app/dist ./dist

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin dashboard \
    && chown -R dashboard:dashboard /app
USER dashboard

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; port=os.getenv('DATABRICKS_APP_PORT') or os.getenv('PORT') or '8000'; urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3).read()"]

CMD ["python", "run.py"]
