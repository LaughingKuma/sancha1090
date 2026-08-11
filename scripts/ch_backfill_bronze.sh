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

# Pause blocks scheduling but not in-flight loaders, which would race the truncate/marker reset
# (double-inserts). Drain RUNNING runs; queued runs of a paused DAG cannot start, so that's complete.
running() {
  local out
  out="$(af dags list-runs "$1" --state running -o plain 2>/dev/null)" \
    || { echo "ERROR: list-runs failed for $1 — refusing to reset blind" >&2; return 1; }
  grep -c "$1" <<<"$out" || true  # grep no-match rc=1 IS the drained case; only the CLI may fail us
}
# 10 min ≈ 3x the longest loader run ever recorded (207 s; docs/notes/2026-08-10-ch-backfill-drain-gate.md)
for d in "${DAGS[@]}"; do
  for _ in $(seq 1 120); do
    n="$(running "$d")" || exit 1
    [ "$n" = 0 ] && continue 2
    sleep 5
  done
  echo "ERROR: $d still has running dag-runs after 10 min — aborting before reset" >&2
  exit 1
done

docker exec -e PYTHONPATH=/opt/airflow "$CONTAINER" python -m include.clickhouse "$@"
