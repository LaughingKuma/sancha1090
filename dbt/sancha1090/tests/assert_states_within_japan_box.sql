-- Untagged: runs with stg_states under transform_marts (--exclude tag:adsb).
-- Pins the v5.0 Japan scope — fails if any staged state vector sits outside the
-- Japan+ocean box (mirrors include/regions.py / stg_states.sql, kept in sync by hand).
select icao24, snapshot_time, latitude, longitude, region
from {{ ref('stg_states') }}
where latitude  not between 20 and 50
   or longitude not between 122 and 165
