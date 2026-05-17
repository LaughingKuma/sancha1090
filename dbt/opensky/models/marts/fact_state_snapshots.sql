{{ config(materialized='table') }}

select
    icao24,
    snapshot_time,
    region,
    callsign,
    origin_country,
    longitude,
    latitude,
    baro_altitude_m,
    velocity_mps,
    track_deg,
    vertical_rate_mps,
    on_ground,
    -- Derived time dimensions for fast grouping
    date_trunc('hour', snapshot_time) as snapshot_hour,
    date_trunc('day', snapshot_time)  as snapshot_day,
    extract(epoch from snapshot_time)::bigint as snapshot_epoch
from {{ ref('stg_states') }}
where latitude is not null
  and longitude is not null