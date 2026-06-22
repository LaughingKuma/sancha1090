{{ config(
    materialized='table',
    properties=(none if target.type == 'clickhouse' else {
        'format': "'PARQUET'",
        'partitioning': "ARRAY['day(snapshot_time)']",
        'sorted_by': "ARRAY['snapshot_time DESC']"
    })
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
    {% if target.type == 'clickhouse' %}toStartOfHour(snapshot_time){% else %}date_trunc('hour', snapshot_time){% endif %} as snapshot_hour
from {{ ref('stg_states') }}
where latitude is not null
  and longitude is not null
