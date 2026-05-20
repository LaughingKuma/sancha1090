#!/bin/bash
# Tear down the opensky-collector VPS and trigger the backfill DAG to drain
# anything it captured into Garage. Auto-sources ../.env if needed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/../.env"

if [ -z "${R2_ENDPOINT:-}" ] && [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

SERVER_NAME="${SERVER_NAME:-opensky-collector}"
BACKFILL_DAG="${BACKFILL_DAG:-backfill_from_buffer}"
SCHEDULER_CONTAINER="${SCHEDULER_CONTAINER:-opensky-airflow-scheduler-1}"

if hcloud server describe "$SERVER_NAME" >/dev/null 2>&1; then
    hcloud server delete "$SERVER_NAME"
    echo "server $SERVER_NAME deleted."
else
    echo "server $SERVER_NAME does not exist; skipping delete."
fi

if docker ps --format '{{.Names}}' | grep -qx "$SCHEDULER_CONTAINER"; then
    echo "triggering $BACKFILL_DAG to drain R2 into Garage..."
    docker exec "$SCHEDULER_CONTAINER" airflow dags unpause "$BACKFILL_DAG" >/dev/null 2>&1 || true
    docker exec "$SCHEDULER_CONTAINER" airflow dags trigger "$BACKFILL_DAG" >/dev/null
    echo "backfill triggered. watch progress in Airflow UI or via dag_run table."
else
    echo "scheduler container '$SCHEDULER_CONTAINER' not running; trigger $BACKFILL_DAG manually once it's up."
fi
