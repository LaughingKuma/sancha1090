# Workbench assets: built in a pinned node image so the Python image never carries node.
FROM node:24-slim@sha256:3638d9a6fe4030bd716be989438248074489337ba3275657f93595428be4fc03 AS assets
USER node
WORKDIR /home/node/app
COPY --chown=node:node package.json package-lock.json .npmrc ./
RUN npm ci --ignore-scripts
COPY --chown=node:node vite.workbench.config.js wb_contract.json ./
COPY --chown=node:node src ./src
# baked into build.json so /healthz can name the commit the bundle came from
ARG GIT_SHA=unknown
RUN GIT_SHA=$GIT_SHA npm run build

FROM python:3.12-slim

WORKDIR /app
# Drop root: the service only reads RW and serves static files
RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py estimator.py est_serving.py est_route.py ladd.py pathfusion.py cache.py ratelimit.py routes_live.py routes_aircraft.py routes_path.py workbench.py routes_workbench.py ./
COPY wb_store.py wb_models.py wb_contract.json ./
COPY static ./static
COPY --from=assets /home/node/app/static ./static
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Single worker: one in-process poller owns the shared snapshot cache
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
