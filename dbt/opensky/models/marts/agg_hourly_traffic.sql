{{ config(materialized='table') }}

-- Hourly time-series: aircraft activity by hour.
-- Powers the "traffic over time" line chart in Superset.

select
    snapshot_hour,
    count(distinct icao24)                       as unique_aircraft,
    count(*)                                     as total_observations,
    sum(case when not on_ground then 1 else 0 end) as airborne_observations,
    sum(case when on_ground then 1 else 0 end)     as on_ground_observations,
    cast(avg(case when not on_ground then velocity_mps * 3.6 end) as decimal(10, 2)) as avg_airborne_speed_kmh
from {{ ref('fact_state_snapshots') }}
group by snapshot_hour
order by snapshot_hour
