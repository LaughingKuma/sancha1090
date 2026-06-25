{{ config(
    materialized='table',
    properties=none
) }}

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
    toStartOfHour(snapshot_time) as snapshot_hour
from {{ ref('stg_states') }}
where latitude is not null
  and longitude is not null
