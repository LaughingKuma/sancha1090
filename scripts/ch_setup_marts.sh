#!/usr/bin/env bash
set -euo pipefail
# One-time marts-lane setup for ClickHouse, run once at deploy (the per-tick transform_adsb_silver
# DAG only does the incremental dbt_run_ch; the reference data below is loaded once):
#   1. seed the CH dims  -> dim.dim_hex_country + silver_ch.dim_airlines/dim_airports/dim_route_overrides
#   2. load bronze.aircraft_db + bronze.adsblol_states (the per-tick loaders feed adsb/states/flights only,
#      not these static/frozen tables) + reload the dict
# Idempotent and best-effort; safe to re-run.
#
#   scripts/ch_setup_marts.sh
#
# Re-runnable: dbt seed replaces the seed tables; the aircraft_db + adsblol_states loads truncate first.

CONTAINER="${SCHEDULER_CONTAINER:-sancha1090-airflow-scheduler-1}"
DBT_DIR="/opt/airflow/dbt/sancha1090"

echo ">> seeding CH dims (dim.dim_hex_country, silver_ch.dim_airlines, silver_ch.dim_airports, silver_ch.dim_route_overrides)"
docker exec "$CONTAINER" bash -c \
  "cd $DBT_DIR && dbt seed --select tag:adsb dim_route_overrides --target clickhouse --profiles-dir . --no-use-colors"

echo ">> loading bronze.aircraft_db + bronze.adsblol_states + reloading the hex-country dict"
docker exec -e PYTHONPATH=/opt/airflow "$CONTAINER" python -m include.clickhouse --marts
