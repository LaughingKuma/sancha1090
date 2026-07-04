#!/usr/bin/env bash
set -euo pipefail
# One-time lane-naming migration (2026-07-03 spec): bronze.archive_states -> bronze.adsblol_states,
# Garage bronze/archive_states_raw/ -> bronze/adsblol_states_raw/, adsb provenance 'backfill' -> 'pre_cutover'.
#   --pre  : run BEFORE the rename code is active (compat view keeps old-name reads green).
#   --post : run AFTER the PR merges (drop compat view + old objects + stale dbt relations).

CH="docker exec -i sancha1090-clickhouse-1 clickhouse-client"
PG="docker exec -i sancha1090-postgres-analytics-1 psql -U analytics -d analytics -v ON_ERROR_STOP=1"
SCHED="docker exec -i -e PYTHONPATH=/opt/airflow sancha1090-airflow-scheduler-1"

pre() {
  # Compat view: un-merged code (and a main checkout) keeps SELECTing the old name; dropped in --post.
  # One -mn invocation: no window where the old name resolves to nothing if the view DDL fails.
  $CH -mn -q "RENAME TABLE bronze.archive_states TO bronze.adsblol_states;
CREATE VIEW bronze.archive_states AS SELECT * FROM bronze.adsblol_states;"

  $SCHED python - <<'PY'
from include.s3_helpers import get_bucket, get_s3fs
fs, bucket = get_s3fs(), get_bucket()
old_prefix = f"{bucket}/bronze/archive_states_raw/"
new_prefix = f"{bucket}/bronze/adsblol_states_raw/"
src = [k for k in fs.find(old_prefix)]
assert src, "nothing under bronze/archive_states_raw"
expected = {k.replace("/archive_states_raw/", "/adsblol_states_raw/", 1): fs.info(k)["size"] for k in src}
for key in src:
    fs.copy(key, key.replace("/archive_states_raw/", "/adsblol_states_raw/", 1))
new = set(fs.find(new_prefix))
missing = sorted(set(expected) - new)
mismatched = sorted(k for k, size in expected.items() if k in new and fs.info(k)["size"] != size)
assert not missing, f"copy mismatch: missing {missing[:10]}"
assert not mismatched, f"copy mismatch: size mismatch for {mismatched[:10]}"
print(f"copied {len(expected)} objects")
PY

  # Manifest URIs follow the objects; object_uri is the PK, the UPDATE keeps ch_loaded_at/archived_at identity.
  $PG -c "UPDATE public.ingestion_manifest
             SET object_uri = replace(object_uri, '/bronze/archive_states_raw/', '/bronze/adsblol_states_raw/')
           WHERE object_uri LIKE '%/bronze/archive\_states\_raw/%'"

  # Provenance relabel: value has had no writer since the one-shot v3.3 backfill_adsb DAG was deleted.
  $PG -c "UPDATE public.adsb_ingestion_manifest SET provenance = 'pre_cutover' WHERE provenance = 'backfill'"
}

post() {
  $CH -q "DROP VIEW IF EXISTS bronze.archive_states"
  # dbt built the renamed relations; the old-name tables are unreferenced leftovers.
  $CH -q "DROP TABLE IF EXISTS silver_ch.stg_states_history"
  $CH -q "DROP TABLE IF EXISTS gold_ch.agg_hourly_traffic_history"
  $CH -q "DROP TABLE IF EXISTS gold_ch.agg_hourly_traffic_live_archive"
  $CH -q "DROP TABLE IF EXISTS silver_ch.int_adsb_callsign_backfill"

  $SCHED python - <<'PY'
from include.s3_helpers import get_bucket, get_s3fs
fs, bucket = get_s3fs(), get_bucket()
old_prefix = f"{bucket}/bronze/archive_states_raw/"
new_prefix = f"{bucket}/bronze/adsblol_states_raw/"
old = [k for k in fs.find(old_prefix)]
new = set(fs.find(new_prefix))
assert new, "refusing delete: new prefix not populated"
expected = {k.replace("/archive_states_raw/", "/adsblol_states_raw/", 1): fs.info(k)["size"] for k in old}
missing = sorted(set(expected) - new)
mismatched = sorted(k for k, size in expected.items() if k in new and fs.info(k)["size"] != size)
assert not missing, f"refusing delete: missing copied objects {missing[:10]}"
assert not mismatched, f"refusing delete: size mismatch for {mismatched[:10]}"
for key in old:
    fs.rm(key)
print(f"deleted {len(old)} old-prefix objects")
PY
  # Optional: airflow metadata row for the renamed DAG.
  # docker exec sancha1090-airflow-scheduler-1 airflow dags delete backfill_from_buffer -y
}

case "${1:-}" in
  --pre) pre ;;
  --post) post ;;
  *) echo "usage: $0 --pre|--post" >&2; exit 2 ;;
esac
