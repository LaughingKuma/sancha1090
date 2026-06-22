-- v5.7: raw positions for /track — trail state lives in RW, not the sidecar, so livemap
-- restarts keep 30-min tracks. No dim joins: trails need hex/pos/alt only.
-- New relation → plain IF NOT EXISTS suffices; a future shape change needs its own
-- drop-sentinel (see 03_mv_current_aircraft.sql for the pattern).
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_track_positions AS
WITH typed AS (
    SELECT
        j ->> 'hex'                                   AS hex,
        to_timestamp((j ->> 'now')::double precision) AS capture_ts,
        (j ->> 'lon')::double precision               AS lon,
        (j ->> 'lat')::double precision               AS lat,
        j ->> 'alt_baro'                              AS alt_baro  -- varchar like bronze: carries 'ground'
    FROM (SELECT convert_from(data, 'utf-8')::jsonb AS j FROM adsb_live) raw
)
SELECT hex, capture_ts, lon, lat, alt_baro
FROM typed
WHERE hex IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL
  -- temporal filter: rows expire on their own; 1800 s = the selected-track window map.js prunes to
  AND capture_ts > now() - interval '1800 seconds';
