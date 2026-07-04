#!/usr/bin/env bash
set -euo pipefail
# v6.3 one-time migration: rebuild bronze.adsb_states with the new DDL (ReplacingMergeTree + ZSTD/T64 codecs +
# the baked db_flags Int32 column + _raw_json ELIMINATED). Unlike P8a's in-place engine swap, the schema CHANGES
# (db_flags replaces _raw_json), so this RELOADS FROM SOURCE — the Garage Parquet (bronze/adsb_state/**) holds the
# full history (GC off) and CH==Garage grain was verified delta-0, so a re-sweep is lossless. Only the live box
# needs this; a fresh deploy applies the new DDL directly via clickhouse-init.
#
#   scripts/ch_migrate_adsb_rebuild.sh              # build _new, verify, EXCHANGE, reseed MVs; leaves tableize_adsb PAUSED
#   scripts/ch_migrate_adsb_rebuild.sh --build-only # stop after verify (inspect _new before swapping)
#
# Safe to abort before the EXCHANGE: nothing touches the live table until the exact verify gate passes; on any
# failure the scratch _new is dropped and prod is left exactly as-is. The source Parquet is the rollback.
#
# Pauses tableize_adsb for the run (the only DAG that INSERTs adsb_states) so no tick lands during the rebuild;
# the trap restores exactly the set it paused. NO drain step: the full s3() sweep already loads every landed
# Parquet, and the NEW loader bakes db_flags so it can't insert into the OLD (_raw_json) table anyway.

SCHED="${SCHEDULER_CONTAINER:-sancha1090-airflow-scheduler-1}"
CH="${CH_CONTAINER:-sancha1090-clickhouse-1}"
BRONZE_SQL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/clickhouse/sql/01_bronze.sql"
BUILD_ONLY=0; [[ "${1:-}" == "--build-only" ]] && BUILD_ONLY=1

chq() { docker exec -i "$CH" clickhouse-client "$@"; }
af()  { docker exec "$SCHED" airflow "$@"; }
py()  { docker exec -e PYTHONPATH=/opt/airflow -u airflow "$SCHED" python -c "$1"; }
is_paused() { af dags details tableize_adsb -o yaml 2>/dev/null | grep -qiE "is_paused:[[:space:]]*'?true"; }

PAUSED_BY_US=0; SWAPPED=0; KEEP_NEW=0
restore() {
  local status=$?
  # Pre-swap failure: drop the partial _new so the next run's preflight isn't blocked (--build-only keeps it).
  [[ "$status" != 0 && "$SWAPPED" == 0 && "$KEEP_NEW" == 0 ]] && chq -q "DROP TABLE IF EXISTS bronze.adsb_states_new" >/dev/null 2>&1 || true
  # Only auto-unpause BEFORE the swap; after EXCHANGE leave tableize_adsb PAUSED for operator verify/rollback.
  [[ "$PAUSED_BY_US" == 1 && "$SWAPPED" == 0 ]] && af dags unpause tableize_adsb >/dev/null 2>&1 || true
}
trap restore EXIT

# 0. PREFLIGHT — a leftover _new/_old from a prior (un-cleaned) run would corrupt the swap. Refuse rather than
# silently drop; the operator verifies the gate is green and drops _old first.
if [[ "$(chq -q "EXISTS TABLE bronze.adsb_states_new")" == "1" ]]; then
  echo "ABORT: bronze.adsb_states_new already exists (prior run not cleaned up). Drop it, then re-run." >&2; exit 1
fi
if [[ "$(chq -q "EXISTS TABLE bronze.adsb_states_old")" == "1" ]]; then
  echo "ABORT: bronze.adsb_states_old already exists (prior migration not cleaned up). Verify the gate is green, then DROP it before re-running." >&2; exit 1
fi

# 1. QUIESCE — no `|| true`: a pause that fails to take must abort before we rebuild.
if ! is_paused; then af dags pause tableize_adsb >/dev/null; PAUSED_BY_US=1; fi

# 2. BUILD NEW from the updated DDL (db_flags + RMT + codecs). MV-safe: the MVs are bound to adsb_states, not _new.
echo ">> building bronze.adsb_states_new from the v6.3 DDL"
chq -q "DROP TABLE IF EXISTS bronze.adsb_states_new"
sed -n '/^CREATE TABLE IF NOT EXISTS bronze.adsb_states$/,/SETTINGS index_granularity = 8192, allow_nullable_key = 1;/p' "$BRONZE_SQL" \
  | sed 's/bronze.adsb_states$/bronze.adsb_states_new/' \
  | chq --multiquery

# 3. RELOAD FROM SOURCE — rebuild_adsb_from_garage projects the Parquet + bakes db_flags from _raw_json into _new
# (~23M rows). mark=False: the data is in the SCRATCH table, not live, so the manifest must NOT advance (an
# abort/--build-only would strand files); after the swap the per-tick loader replays any still-pending files (RMT
# collapses the dup).
echo ">> reloading bronze.adsb_states_new from the Garage Parquet (bakes db_flags; a few minutes)"
py 'import json; from include import clickhouse as c; print(json.dumps(c.rebuild_adsb_from_garage(target_table="adsb_states_new", mark=False), default=str))'

# 4. COLLAPSE any crash-window replay from the reload (RMT). Source is unique, so this is a no-op backstop.
echo ">> OPTIMIZE FINAL"
chq -q "OPTIMIZE TABLE bronze.adsb_states_new FINAL"

# 5. VERIFY GATE — EXACT, no tolerance. capture_ts is Float64 epoch sec (compare bare, NOT toDateTime). Three checks:
#   (a) fully merged: rows == distinct (hex,capture_ts);
#   (b) complete vs source: closed-window distinct grain == the Garage Parquet's;
#   (c) db_flags decode parity on the COMMON grains: _new's full-int db_flags fingerprint, restricted to grains the
#       live table has, == live's JSONExtractInt(_raw_json) — common-grain because live lags source by the pending
#       files the new loader can't write to the old table (so _new is a superset; raw equality would false-fail).
CUTOFF=$(chq -q "SELECT toUInt32(now('UTC')) - 7200")   # 2h closed window, captured once, both sides
NEW=$(chq    -q "SELECT count() FROM bronze.adsb_states_new")
NEWDIST=$(chq -q "SELECT uniqExact((hex, capture_ts)) FROM bronze.adsb_states_new")
NEWCW=$(chq  -q "SELECT uniqExact((hex, capture_ts)) FROM bronze.adsb_states_new WHERE capture_ts < ${CUTOFF}")
SRCCW=$(chq  -q "SELECT uniqExact((hex, capture_ts)) FROM s3(garage, filename='bronze/adsb_state/**/*.parquet', format='Parquet') WHERE capture_ts < ${CUTOFF}")
# GROUP BY (hex,capture_ts) both sides — the pre-RMT old table can carry duplicate-grain rows a raw XOR would cancel
# (masking drift); project to grain first. max() not any() so the per-grain pick is deterministic; transform_null_in=1
# so a NULL-hex grain matches the IN (default NULL-in semantics would drop it from _new but not the old side).
DBNEW=$(chq -q "SELECT groupBitXor(cityHash64(toString(tuple(hex, capture_ts, dbf)))) FROM (SELECT hex, capture_ts, max(toInt32(db_flags)) AS dbf FROM bronze.adsb_states_new WHERE capture_ts < ${CUTOFF} AND (hex, capture_ts) IN (SELECT hex, capture_ts FROM bronze.adsb_states WHERE capture_ts < ${CUTOFF}) GROUP BY hex, capture_ts) SETTINGS transform_null_in = 1")
DBOLD=$(chq -q "SELECT groupBitXor(cityHash64(toString(tuple(hex, capture_ts, dbf)))) FROM (SELECT hex, capture_ts, max(toInt32(JSONExtractInt(_raw_json, 'dbFlags'))) AS dbf FROM bronze.adsb_states WHERE capture_ts < ${CUTOFF} GROUP BY hex, capture_ts)")
echo ">> new=$NEW new_distinct=$NEWDIST | closed_grain new=$NEWCW src=$SRCCW | db_flags_fp new=$DBNEW old=$DBOLD"
fail() { echo "ABORT: $1" >&2; chq -q "DROP TABLE IF EXISTS bronze.adsb_states_new"; exit 1; }
[[ "$NEW"    == "$NEWDIST" ]] || fail "new not fully merged (rows $NEW != distinct $NEWDIST)"
[[ "$NEWCW"  == "$SRCCW"   ]] || fail "new incomplete vs source (closed grain $NEWCW != $SRCCW)"
[[ "$DBNEW"  == "$DBOLD"   ]] || fail "db_flags decode drift on common grains (fingerprint $DBNEW != live _raw_json $DBOLD)"
echo ">> verify gate PASSED (exact)"

if [[ "$BUILD_ONLY" == 1 ]]; then
  KEEP_NEW=1   # leave _new for inspection; the trap must not drop it
  echo ">> --build-only: bronze.adsb_states_new built + verified; EXCHANGE skipped. Inspect it; a normal rerun first"
  echo ">>   needs:  docker exec -i $CH clickhouse-client -q \"DROP TABLE bronze.adsb_states_new\"  (the preflight refuses a leftover _new)."
  exit 0
fi

# 6. EXCHANGE (atomic on the Atomic 'bronze' db) + keep the old table for rollback. tableize_adsb stays PAUSED so
# no tick lands during the swap. Mirrors P8a: EXCHANGE then RENAME _new (now holding the OLD rows) to _old.
echo ">> EXCHANGE TABLES + keep _old"
chq -q "EXCHANGE TABLES bronze.adsb_states AND bronze.adsb_states_new"
SWAPPED=1   # past this point the trap leaves tableize_adsb PAUSED and never drops a table (rollback is manual)
chq -q "RENAME TABLE bronze.adsb_states_new TO bronze.adsb_states_old"

# 7. RESEED the two re-grained ADS-B MVs (their target schema changed: + snapshot_hour, uniq->uniqExact, +90d TTL),
# scoped via names= so the OpenSky MVs are untouched (no recreate gap for the live states lane). The MV bodies now
# read db_flags from the just-swapped live adsb_states. NOTE: the serving views blip for the duration of the seed
# (a full re-scan, minutes) — run off-peak; ch_serving_parity may red transiently and self-recover.
echo ">> reseeding the ADS-B MVs (drop old-schema targets, recreate hourly + reseed from the rebuilt adsb_states)"
chq -q "DROP TABLE IF EXISTS gold_ch.agg_country_traffic_adsb_acc"
chq -q "DROP TABLE IF EXISTS gold_ch.agg_airline_traffic_adsb_acc"
py 'import json; from include import ch_incremental_mvs as m; print(json.dumps(m.apply(reseed=True, names=("agg_country_traffic_adsb_acc","agg_airline_traffic_adsb_acc")), default=str))'

PAUSED_BY_US=0   # leave tableize_adsb PAUSED for the operator so the rollback window stays delta-free
echo ">> DONE. live bronze.adsb_states is now ReplacingMergeTree + codecs + db_flags ($NEW rows). tableize_adsb is STILL PAUSED."
echo ">> 1) verify the gates:  docker exec -e PYTHONPATH=/opt/airflow $SCHED python -m include.ch_parity"
echo ">>                       docker exec -e PYTHONPATH=/opt/airflow $SCHED python -m include.ch_served_value"
echo ">> 2a) GREEN -> resume:  docker exec $SCHED airflow dags unpause tableize_adsb"
echo ">> 2b) RED -> rollback (table, while still paused): EXCHANGE back to _old, then revert the v6.3 code deploy"
echo ">>        (the deployed MV/loader code reads db_flags, so a table-only rollback needs the prior image too):"
echo ">>        docker exec -i $CH clickhouse-client -q \"EXCHANGE TABLES bronze.adsb_states AND bronze.adsb_states_old\""
echo ">>        then redeploy the pre-v6.3 image and re-run the MV apply; OR re-run this migration from the intact source Parquet."
echo ">> CLEANUP after >= 2h of a green gate: docker exec -i $CH clickhouse-client -q \"DROP TABLE bronze.adsb_states_old\""
