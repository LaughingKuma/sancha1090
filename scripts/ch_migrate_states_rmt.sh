#!/usr/bin/env bash
set -euo pipefail
# P8a one-time migration: rebuild bronze.opensky_states as ReplacingMergeTree so the +977K crash-window
# replay surplus collapses to source-exact (the new DDL is already in clickhouse/sql/01_bronze.sql, but
# CREATE IF NOT EXISTS won't re-engine an existing table — a live table must be rebuilt + swapped).
#
#   scripts/ch_migrate_states_rmt.sh             # build _new, OPTIMIZE FINAL, verify, EXCHANGE, keep _old
#   scripts/ch_migrate_states_rmt.sh --build-only # stop after verify (inspect _new before swapping)
#
# Safe to abort: nothing touches the live table until the verification gate passes; on any check failure the
# scratch _new is dropped and prod is left exactly as-is. After the EXCHANGE the pre-migration MergeTree table
# is kept as bronze.opensky_states_old for rollback — drop it only after >= one freshness window (2h) of a
# green ch_serving_parity gate. The 2 P4 MVs are name-bound to opensky_states and grain-idempotent, so the
# EXCHANGE needs no MV reseed.
#
# Pauses tableize_states for the run (the only DAG that INSERTs opensky_states) so no tick lands in the old
# table between the snapshot copy and the EXCHANGE; the trap restores exactly the set it paused.

SCHED="${SCHEDULER_CONTAINER:-sancha1090-airflow-scheduler-1}"
CH="${CH_CONTAINER:-sancha1090-clickhouse-1}"
BRONZE_SQL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/clickhouse/sql/01_bronze.sql"
BUILD_ONLY=0; [[ "${1:-}" == "--build-only" ]] && BUILD_ONLY=1

chq() { docker exec -i "$CH" clickhouse-client "$@"; }
af()  { docker exec "$SCHED" airflow "$@"; }
is_paused() { af dags details tableize_states -o yaml 2>/dev/null | grep -qiE "is_paused:[[:space:]]*'?true"; }

PAUSED_BY_US=0
restore() { [[ "$PAUSED_BY_US" == 1 ]] && af dags unpause tableize_states >/dev/null 2>&1 || true; }
trap restore EXIT

# 0. PREFLIGHT — a leftover _old from a prior (un-cleaned) migration would make the post-EXCHANGE RENAME fail
# mid-swap. Refuse rather than silently drop the rollback table; the operator must verify + drop it first.
if [[ "$(chq -q "EXISTS TABLE bronze.opensky_states_old")" == "1" ]]; then
  echo "ABORT: bronze.opensky_states_old already exists (prior migration not cleaned up). Verify the gate is green, then DROP it before re-running." >&2
  exit 1
fi

# 1. QUIESCE — no `|| true`: a pause that fails to take must abort before we snapshot the table.
if ! is_paused; then af dags pause tableize_states >/dev/null; PAUSED_BY_US=1; fi

# 2. DRAIN any CH-pending states into the OLD table so the snapshot copy is current, then require zero pending.
docker exec -e PYTHONPATH=/opt/airflow "$SCHED" python -c \
  'import json; from include import clickhouse as c; print(json.dumps(c.load_states_pending_to_ch(), default=str))'
PENDING=$(docker exec -e PYTHONPATH=/opt/airflow "$SCHED" python -c \
  'from include import manifest as m; print(sum(len(m.pending_ch_uris(p)) for p in ("bronze/states","bronze/states_raw")))')
if [[ "$PENDING" != "0" ]]; then echo "ABORT: $PENDING states URIs still CH-pending after drain" >&2; exit 1; fi

# 3. BUILD NEW (local copy, MV-safe: the MVs are bound to opensky_states, not _new, so they don't fire here).
echo ">> building bronze.opensky_states_new from the RMT DDL"
chq -q "DROP TABLE IF EXISTS bronze.opensky_states_new"
sed -n '/^CREATE TABLE IF NOT EXISTS bronze.opensky_states$/,/SETTINGS allow_nullable_key = 1;/p' "$BRONZE_SQL" \
  | sed 's/bronze.opensky_states$/bronze.opensky_states_new/' \
  | chq --multiquery
chq -q "INSERT INTO bronze.opensky_states_new SELECT * FROM bronze.opensky_states"

# 4. COLLAPSE the replays.
echo ">> OPTIMIZE FINAL"
chq -q "OPTIMIZE TABLE bronze.opensky_states_new FINAL"

# 5. VERIFY GATE — EXACT, no tolerance (abort + drop _new on any failure; prod untouched). Three exact checks:
#   (a) new fully merged: rows == distinct fp (no replay left);
#   (b) lossless dedup: new == the deduped OLD (uniqExact of the same fp expr on the pre-migration table) — the
#       rebuild removed ONLY replays, gained/lost nothing. Self-contained, no source-timing dependency;
#   (c) complete vs source: closed-window distinct CONTENT == source's — distinct content (NOT grain) so a lost
#       same-grain recapture is visible (grain would hide it), over a SINGLE captured cutoff used on both sides
#       (no hour-boundary race). The < now()-2h window excludes the trail, so equality holds with ingest running.
FP="cityHash64(toString(tuple(icao24, callsign, origin_country, time_position, last_contact, longitude, latitude, baro_altitude, on_ground, velocity, true_track, vertical_rate, geo_altitude, squawk, spi, position_source, snapshot_time, region, ingested_at)))"
CUTOFF=$(chq -q "SELECT toUnixTimestamp(toStartOfHour(now('UTC')) - INTERVAL 2 HOUR)")   # captured once, both sides
NEW=$(chq -q "SELECT count() FROM bronze.opensky_states_new")
NEWFP=$(chq -q "SELECT uniqExact(_dedup_fp) FROM bronze.opensky_states_new")
OLDFP=$(chq -q "SELECT uniqExact($FP) FROM bronze.opensky_states")
NEWCONTENT=$(chq -q "SELECT uniqExact($FP) FROM bronze.opensky_states_new WHERE snapshot_time < toDateTime($CUTOFF, 'UTC')")
SRCCONTENT=$(chq -q "SELECT uniqExact($FP) FROM s3(garage, filename='bronze/{states,states_raw}/**/*.parquet', format='Parquet') WHERE snapshot_time < $CUTOFF")
echo ">> new=$NEW new_distinct_fp=$NEWFP deduped_old=$OLDFP | closed_content new=$NEWCONTENT src=$SRCCONTENT"
fail() { echo "ABORT: $1" >&2; chq -q "DROP TABLE IF EXISTS bronze.opensky_states_new"; exit 1; }
[[ "$NEW" == "$NEWFP" ]]          || fail "new not fully merged (rows $NEW != distinct fp $NEWFP)"
[[ "$NEW" == "$OLDFP" ]]          || fail "new != deduped old (rebuild lost/gained rows: $NEW vs $OLDFP)"
[[ "$NEWCONTENT" == "$SRCCONTENT" ]] || fail "new incomplete vs source (closed-window content: $NEWCONTENT != $SRCCONTENT)"
echo ">> verify gate PASSED (exact)"

if [[ "$BUILD_ONLY" == 1 ]]; then
  echo ">> --build-only: bronze.opensky_states_new built + verified; EXCHANGE skipped. Inspect, then re-run without --build-only."
  exit 0
fi

# 6. EXCHANGE (atomic on the Atomic 'bronze' db) + keep the old MergeTree table for rollback. Ingestion is left
# PAUSED on success: that keeps the rollback window DELTA-FREE. A plain EXCHANGE-back is lossless ONLY while no
# post-swap rows have been ingested — once tableize_states resumes it loads new rows into the RMT table (and
# marks them ch_loaded), so an EXCHANGE-back after unpausing would silently drop that delta. The operator
# verifies the gate, then either resumes (commit) or EXCHANGEs back (rollback) — both while still paused.
echo ">> EXCHANGE TABLES + keep _old"
chq -q "EXCHANGE TABLES bronze.opensky_states AND bronze.opensky_states_new"
chq -q "RENAME TABLE bronze.opensky_states_new TO bronze.opensky_states_old"
PAUSED_BY_US=0   # leave tableize_states PAUSED for the operator so the rollback window stays delta-free
echo ">> DONE. live bronze.opensky_states is now ReplacingMergeTree ($NEW rows). tableize_states is STILL PAUSED."
echo ">> 1) verify the gate:  docker exec -e PYTHONPATH=/opt/airflow $SCHED python -m include.ch_parity"
echo ">> 2a) GREEN -> resume:  docker exec $SCHED airflow dags unpause tableize_states"
echo ">> 2b) RED -> rollback (lossless ONLY while still paused, before any post-swap ingestion):"
echo ">>        docker exec -i $CH clickhouse-client -q \"EXCHANGE TABLES bronze.opensky_states AND bronze.opensky_states_old\" && docker exec $SCHED airflow dags unpause tableize_states"
echo ">> Do NOT EXCHANGE-back after unpausing — post-swap rows would be lost; for a later revert, re-run the migration."
echo ">> CLEANUP after >= 2h of a green gate: docker exec -i $CH clickhouse-client -q \"DROP TABLE bronze.opensky_states_old\""
