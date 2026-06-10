{{ config(materialized='table') }}

-- Hourly time-series: aircraft activity by hour.
-- Powers the "traffic over time" line chart in Superset.
-- v5.2: three disjoint segments — the live window's fresh tail, the insert-only
-- accumulator for everything that has ever settled, and the pre-pipeline backfill
-- for hours nothing native covers. Disjointness keeps snapshot_hour unique.

with live_archive as (
    select * from {{ ref('agg_hourly_traffic_live_archive') }}
),
live_tail as (
    -- Only hours newer than the accumulator: its copy of an hour is complete,
    -- while the live window's oldest hour can be cut mid-hour by the 30-day filter.
    select
        snapshot_hour,
        count(distinct icao24)                       as unique_aircraft,
        count(*)                                     as total_observations,
        sum(case when not on_ground then 1 else 0 end) as airborne_observations,
        sum(case when on_ground then 1 else 0 end)     as on_ground_observations,
        cast(avg(case when not on_ground then velocity_mps * 3.6 end) as decimal(10, 2)) as avg_airborne_speed_kmh
    from {{ ref('fact_state_snapshots') }}
    group by snapshot_hour
    having snapshot_hour > (
        select coalesce(max(snapshot_hour), timestamp '1970-01-01 00:00:00 UTC') from live_archive
    )
),
history as (
    select h.*
    from {{ ref('agg_hourly_traffic_history') }} h
    left join live_archive la on la.snapshot_hour = h.snapshot_hour
    where la.snapshot_hour is null
      and h.snapshot_hour < (
          select coalesce(min(snapshot_hour), timestamp '9999-01-01 00:00:00 UTC') from live_tail
      )
)
select * from live_tail
union all
select * from live_archive
union all
select * from history
order by snapshot_hour
