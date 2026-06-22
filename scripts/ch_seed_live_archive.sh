#!/usr/bin/env bash
set -euo pipefail
# One-time seed of the CH agg_hourly_traffic_live_archive accumulator's pre-window history. A freshly-built
# accumulator only reaches back the 30-day FSS window, so it has a hole between the archive-history max and the
# rolling-window start. This recomputes those settled hours from CH's own
# bronze.opensky_states. Idempotent; best-effort. Run ONCE, AFTER the first transform_marts tick built the table.
#
#   scripts/ch_seed_live_archive.sh

CONTAINER="${SCHEDULER_CONTAINER:-sancha1090-airflow-scheduler-1}"

echo ">> seeding gold_ch.agg_hourly_traffic_live_archive pre-window history from bronze.opensky_states"
docker exec -e PYTHONPATH=/opt/airflow "$CONTAINER" python -m include.clickhouse --seed-live-archive
