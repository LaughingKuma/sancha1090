-- Reads the ClickHouse bronze table directly; columns are already typed, so no ::casts.
-- 30-day filter mirrors retention — without it the mart rebuild would scan all bronze history.
-- Japan scope (v5.0): filter geographically, NOT by region label — the pre-v5.0
-- 'east_asia' box already covered Japan, so a region='japan' filter would drop
-- recent Japan history. Box is the japan_box_* vars (mirrors include/regions.py).
with src as (
    select *
    from {{ source('bronze', 'opensky_states') }}
    where snapshot_time >= now('UTC') - INTERVAL 30 DAY
      and {{ in_japan_box('latitude', 'longitude') }}
),
typed as (
    select
        icao24,
        nullIf(trimBoth(callsign), '')  as callsign,
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
        ingested_at
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
    position_source, snapshot_time, region, ingested_at
from dedup
where rn = 1
