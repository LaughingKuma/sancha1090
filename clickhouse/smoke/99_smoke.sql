-- P1 smoke validation. Run by hand (NOT loaded by clickhouse-init: it lives outside the
-- ./clickhouse/sql glob so a transient Garage hiccup on the s3() read can never red the
-- schema-provisioning container). Run with:
--   docker compose exec -T clickhouse clickhouse-client --multiquery < clickhouse/smoke/99_smoke.sql

-- 1. version smoke
SELECT version();                                   -- expect 26.5.1.*

-- 2. schema exists, empty
SELECT count() FROM bronze.adsb_states;             -- 0
SELECT count() FROM bronze.opensky_states;          -- 0
SELECT name FROM system.tables WHERE database IN ('bronze','dim') ORDER BY 1;
-- expect 5 bronze + 1 dim table + the dictionary (dictionaries also list in system.tables):
--   adsb_states, adsblol_states, aircraft_db, opensky_flights, opensky_states,
--   dim_hex_country, dict_hex_country

-- 3. column-count sanity on the big table: 60 logical cols (include/adsb_schema.py CH_ADSB_COLUMNS —
--    v6.3 swapped _raw_json for the baked db_flags) + capture_date MATERIALIZED = 61.
SELECT count() FROM system.columns WHERE database='bronze' AND table='adsb_states';   -- 61

-- 4. partition driver is the materialized Date, not the float epoch
SELECT type, default_kind FROM system.columns
WHERE database='bronze' AND table='adsb_states' AND name='capture_date';   -- Date / MATERIALIZED

-- 5. dictionary loaded, range_hashed (empty source = 0 elements, status LOADED)
SELECT name, type, status FROM system.dictionaries WHERE name='dict_hex_country';
-- expect: dict_hex_country / RangeHashed / LOADED (element_count 0 until P3 seeds it)

-- 6. s3() smoke read via the NAMED COLLECTION (no inline creds — spike gotcha #2).
-- Use the `filename` key (appended to the collection's base url; `path` is not a valid s3()
-- named-collection key on 26.5.1), and a recursive `**` glob — the edge Parquet is date-
-- partitioned at bronze/adsb_state/dt=YYYY-MM-DD/*.parquet, so a flat *.parquet matches nothing.
SELECT count() FROM s3(garage, filename='bronze/adsb_state/**/*.parquet', format='Parquet');
-- expect: ~19.3M+ (the live edge Parquet; read-only, nothing written to CH)
SELECT max(r_dst) FROM s3(garage, filename='bronze/adsb_state/**/*.parquet', format='Parquet');
-- expect: >= 166.453 (spike Q2 answer; live data only grows the furthest-signal max)
