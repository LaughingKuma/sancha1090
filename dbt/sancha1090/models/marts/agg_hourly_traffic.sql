{{ config(materialized='table', tags=['ch_mv']) }}

-- Hourly time-series: aircraft activity by hour.
-- Powers the "traffic over time" line chart in Superset.
-- v5.2: three disjoint segments — the live window's fresh tail, the insert-only
-- accumulator for everything that has ever settled, and the adsb.lol history
-- for hours nothing native covers. Disjointness keeps snapshot_hour unique.

with opensky_settled as (
    select * from {{ ref('agg_hourly_traffic_opensky_settled') }}
),
live_tail as (
    -- Only hours newer than the accumulator: its copy of an hour is complete,
    -- while the live window's oldest hour can be cut mid-hour by the 30-day filter.
    select
        snapshot_hour,
        {{ hourly_traffic_measures() }}
    from {{ ref('fact_state_snapshots') }}
    group by snapshot_hour
    having snapshot_hour > (
        select coalesce(max(snapshot_hour), toDateTime('1970-01-01 00:00:00', 'UTC')) from opensky_settled
    )
),
history as (
    select h.*
    from {{ ref('agg_hourly_traffic_adsblol') }} h
    left join opensky_settled os on os.snapshot_hour = h.snapshot_hour
    where os.snapshot_hour is null
      -- An empty live_tail (count()=0) means "include all history",
      -- else keep history strictly older than live_tail's oldest hour.
      and (
          (select count() from live_tail) = 0
          or h.snapshot_hour < (select min(snapshot_hour) from live_tail)
      )
)
select * from live_tail
union all
select * from opensky_settled
union all
select * from history
order by snapshot_hour
