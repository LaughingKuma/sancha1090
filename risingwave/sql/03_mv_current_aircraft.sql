-- Migrate older deployments: CREATE ... IF NOT EXISTS can't change column shape, so an
-- existing risingwave-data volume keeps the old MV and livemap then polls a missing column
-- forever. Sentinels: newest column added (nav_modes) OR a stale stored definition (old
-- staleness window — IF NOT EXISTS can't change that either); either drops the MV (+ its
-- dependent mv_live_counts, which 04 recreates after this file). Bump a sentinel whenever
-- this SELECT changes. Fresh / current volumes skip the drop.
SELECT (
    EXISTS (SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'mv_current_aircraft')
    AND (
        NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'mv_current_aircraft'
                      AND column_name = 'nav_modes')
        OR EXISTS (SELECT 1 FROM rw_catalog.rw_materialized_views
                   WHERE name = 'mv_current_aircraft'
                     AND definition LIKE '%60 seconds%')
    )
)::text AS needs_schema_migration \gset
\if :needs_schema_migration
DROP MATERIALIZED VIEW IF EXISTS mv_live_counts;
DROP MATERIALIZED VIEW IF EXISTS mv_current_aircraft;
\endif

-- Latest state per airframe within the last 120 s, enriched with silver's decode
-- (fct_adsb_state) ported predicate-for-predicate. Silver is canonical: if this and the
-- batch numbers disagree, fix THIS side (the v4 design's Lambda-trap guard).
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_current_aircraft AS
WITH typed AS (
    SELECT
        to_timestamp((j ->> 'now')::double precision) AS capture_ts,
        j ->> 'hex'                                   AS hex,
        j ->> 'flight'                                AS flight,
        (j ->> 'lat')::double precision               AS lat,
        (j ->> 'lon')::double precision               AS lon,
        j ->> 'alt_baro'                              AS alt_baro,  -- varchar like bronze: carries 'ground'
        (j ->> 'gs')::double precision                AS gs,
        (j ->> 'track')::double precision             AS track,
        j ->> 'category'                              AS category,
        j ->> 'r'                                     AS registration,
        j ->> 't'                                     AS typecode,
        j ->> 'desc'                                  AS aircraft_desc,  -- readsb's resolved type name, e.g. 'BOEING 737-800'
        j ->> 'ownOp'                                 AS own_op,   -- registry owner/operator (FAA et al via readsb db)
        j ->> 'year'                                  AS year,     -- varchar like alt_baro: no cast risk
        j ->> 'squawk'                                AS squawk,
        (j ->> 'baro_rate')::double precision          AS baro_rate,   -- barometric V/S, ft/min
        (j ->> 'geom_rate')::double precision          AS geom_rate,   -- geometric V/S fallback when baro absent
        (j ->> 'rssi')::double precision               AS rssi,        -- signal strength, dBFS (rooftop only)
        (j ->> 'nav_altitude_mcp')::double precision   AS nav_altitude_mcp,  -- selected altitude (MCP/FCU)
        j -> 'nav_modes'                               AS nav_modes,   -- autopilot modes (jsonb array), sparse
        -- multi-receiver seam: edge stamps a receiver id later; absent today → 'rooftop'
        coalesce(j ->> 'recv', 'rooftop')             AS recv,
        -- silver: dbFlags are exception flags — absence means FALSE, not unknown
        coalesce((j ->> 'dbFlags')::int, 0)           AS db_flags,
        coalesce(jsonb_array_length(j -> 'mlat'), 0) > 0 AS from_mlat,
        -- silver: try(from_base(lower(hex),16)); the regex guard stands in for try() on
        -- readsb's '~'-prefixed non-ICAO (TIS-B/ADS-R) addresses
        CASE WHEN lower(j ->> 'hex') ~ '^[0-9a-f]{6}$'
             THEN get_byte(decode(lower(j ->> 'hex'), 'hex'), 0) * 65536
                + get_byte(decode(lower(j ->> 'hex'), 'hex'), 1) * 256
                + get_byte(decode(lower(j ->> 'hex'), 'hex'), 2)
        END                                           AS icao_addr
    FROM (SELECT convert_from(data, 'utf-8')::jsonb AS j FROM adsb_live) raw
),
latest AS (
    SELECT * FROM (
        SELECT *, row_number() OVER (PARTITION BY hex ORDER BY capture_ts DESC) AS rn
        FROM typed
        -- temporal filter: rows expire on their own. 120 s = tar1090's measured client-side
        -- position retention; 60 s dropped fringe aircraft (sparse decodes) tar1090 still shows.
        WHERE capture_ts > now() - interval '120 seconds'
    ) ranked
    WHERE rn = 1
)
SELECT
    l.capture_ts,
    l.hex,
    l.flight,
    l.lat,
    l.lon,
    l.alt_baro,
    l.gs,
    l.track,
    l.category,
    l.registration,
    l.typecode,
    -- #34: desc is empty in tar1090-db for some airframes — the 8643 model name is the fallback
    coalesce(nullif(l.aircraft_desc, ''), atype.model_name) AS aircraft_desc,
    l.squawk,
    l.own_op,
    l.year,
    (l.db_flags & 1) <> 0 AS is_military,
    (l.db_flags & 2) <> 0 AS is_interesting,
    (l.db_flags & 4) <> 0 AS is_pia,
    (l.db_flags & 8) <> 0 AS is_ladd,
    l.category = 'A7'     AS is_helicopter,
    CASE WHEN l.from_mlat THEN 'mlat' ELSE 'adsb' END AS position_source,
    al.name      AS airline_name,
    al.country   AS airline_country,
    ctry.country AS reg_country,
    atype.body_class AS body_class,  -- silhouette class for livemap's per-type icons
    l.recv,
    l.baro_rate,
    l.geom_rate,
    l.rssi,
    l.nav_altitude_mcp,
    l.nav_modes
FROM latest l
-- Airline of THIS flight (callsign), a different question than the airframe owner (leasing/codeshare).
LEFT JOIN dim_airlines al
       ON al.icao = substr(trim(l.flight), 1, 3)
      AND trim(l.flight) ~ '^[A-Z]{3}[0-9]'  -- guard: skip GA/registration tails like JA45KA
-- bucket equi-join (RW can't stream non-equi joins); BETWEEN stays as silver's residual predicate
LEFT JOIN dim_hex_country_buckets ctry
       ON ctry.bucket = l.icao_addr / 4096
      AND l.icao_addr BETWEEN ctry.block_lo AND ctry.block_hi
-- type → silhouette class (ICAO Doc 8643 seed); equi-join on typecode, RW-safe
LEFT JOIN dim_aircraft_types atype
       ON atype.typecode = l.typecode;
