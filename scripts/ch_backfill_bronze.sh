#!/usr/bin/env bash
set -euo pipefail
# One-time backfill: load existing Garage bronze Parquet into ClickHouse bronze and mark the
# Postgres manifests CH-loaded, so the per-tick tableize CH tasks no-op on history and only handle
# new data. Idempotent — resets (truncate + clear markers) first unless --no-reset is passed; safe to re-run.
#
#   scripts/ch_backfill_bronze.sh            # full reset + reload
#   scripts/ch_backfill_bronze.sh --no-reset # resume without wiping (only loads CH-pending)
#
# Pauses the three tableize DAGs for the run so a concurrent asset-triggered tick can't double-insert
# the same files (plain MergeTree has no dedup). Only DAGs that were active are paused, and the trap
# restores exactly that set — DAGs you left paused stay paused.

CONTAINER="${SCHEDULER_CONTAINER:-sancha1090-airflow-scheduler-1}"
DAGS=(tableize_states tableize_flights tableize_adsb)

af() { docker exec "$CONTAINER" airflow "$@"; }
is_paused() { af dags details "$1" -o yaml 2>/dev/null | grep -qiE "is_paused:[[:space:]]*'?true"; }

PAUSED_BY_US=()
restore() { for d in "${PAUSED_BY_US[@]:-}"; do af dags unpause "$d" >/dev/null || true; done; }
trap restore EXIT

# No `|| true`: a pause that fails to take must abort before backfill, else it races live ticks.
for d in "${DAGS[@]}"; do
  is_paused "$d" && continue
  af dags pause "$d" >/dev/null
  PAUSED_BY_US+=("$d")
done

docker exec -e PYTHONPATH=/opt/airflow "$CONTAINER" python -m include.clickhouse "$@"
