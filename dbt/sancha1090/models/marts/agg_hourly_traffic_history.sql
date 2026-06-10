{{ config(materialized='table', tags=['history']) }}

-- Pre-pipeline hourly aggregates at the exact agg_hourly_traffic grain, so the
-- live mart can UNION ALL without Superset noticing a new dataset.
select
    date_trunc('hour', snapshot_time)            as snapshot_hour,
    count(distinct icao24)                       as unique_aircraft,
    count(*)                                     as total_observations,
    sum(case when not on_ground then 1 else 0 end) as airborne_observations,
    sum(case when on_ground then 1 else 0 end)     as on_ground_observations,
    cast(avg(case when not on_ground then velocity_mps * 3.6 end) as decimal(10, 2)) as avg_airborne_speed_kmh
from {{ ref('stg_states_history') }}
where latitude is not null
  and longitude is not null
group by 1
