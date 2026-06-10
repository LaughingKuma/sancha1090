{{ config(materialized='table', tags=['history']) }}

-- Backfilled pre-pipeline history (adsb.lol archive, Japan-only at write time).
-- No 30-day filter: this table IS the deep past; it only changes when a backfill
-- wave runs, and it stays small (12-min snapshots, Japan box only).
-- region='japan' only: the full-resolution kanto/kansai rows would distort the
-- per-snapshot trend counts — they're raw material for density/path work, not trends.
with src as (
    select *
    from {{ source('bronze', 'archive_states') }}
    where region = 'japan'
),
typed as (
    select
        icao24,
        nullif(trim(callsign), '')      as callsign,
        origin_country,
        time_position,
        last_contact,
        longitude                       as longitude,
        latitude                        as latitude,
        baro_altitude                   as baro_altitude_m,
        on_ground                       as on_ground,
        velocity                        as velocity_mps,
        true_track                      as track_deg,
        vertical_rate                   as vertical_rate_mps,
        geo_altitude                    as geo_altitude_m,
        squawk,
        spi                             as spi,
        position_source                 as position_source,
        snapshot_time,
        region,
        ingested_at,
        source
    from src
),
dedup as (
    select
        typed.*,
        row_number() over (
            partition by icao24, snapshot_time
            order by ingested_at desc
        ) as rn
    from typed
)
select
    icao24, callsign, origin_country, time_position, last_contact,
    longitude, latitude, baro_altitude_m, on_ground, velocity_mps,
    track_deg, vertical_rate_mps, geo_altitude_m, squawk, spi,
    position_source, snapshot_time, region, ingested_at, source
from dedup
where rn = 1
