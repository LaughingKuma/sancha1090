{{ config(materialized='incremental', incremental_strategy='append') }}

-- Hours age out of stg_states' rolling 30-day window; this insert-only accumulator
-- keeps every settled hour so the trend chart never grows a gap behind the window.
-- Never --full-refresh casually: it would reseed from the current window only,
-- discarding accumulated hours older than 30 days.
with hourly as (
    select
        snapshot_hour,
        count(distinct icao24)                       as unique_aircraft,
        count(*)                                     as total_observations,
        sum(case when not on_ground then 1 else 0 end) as airborne_observations,
        sum(case when on_ground then 1 else 0 end)     as on_ground_observations,
        cast(avg(case when not on_ground then velocity_mps * 3.6 end) as decimal(10, 2)) as avg_airborne_speed_kmh
    from {{ ref('fact_state_snapshots') }}
    group by snapshot_hour
)
select * from hourly
-- Settled = 2h past the hour; live rows land within minutes of their snapshot.
where snapshot_hour < date_trunc('hour', current_timestamp) - interval '2' hour
{% if is_incremental() %}
  and snapshot_hour > (
      select coalesce(max(snapshot_hour), timestamp '1970-01-01 00:00:00 UTC') from {{ this }}
  )
{% endif %}
