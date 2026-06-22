{{ config(materialized='incremental', incremental_strategy='append', tags=['ch_mv']) }}

-- Hours age out of stg_states' rolling 30-day window; this insert-only accumulator
-- keeps every settled hour so the trend chart never grows a gap behind the window.
-- Never --full-refresh casually: it would reseed from the current window only,
-- discarding accumulated hours older than 30 days.
with hourly as (
    select
        snapshot_hour,
        {{ hourly_traffic_measures() }}
    from {{ ref('fact_state_snapshots') }}
    group by snapshot_hour
)
select * from hourly
-- Settled = 2h past the hour; live rows land within minutes of their snapshot.
where snapshot_hour < {% if target.type == 'clickhouse' %}toStartOfHour(now('UTC')) - INTERVAL 2 HOUR{% else %}date_trunc('hour', current_timestamp) - interval '2' hour{% endif %}
{% if is_incremental() %}
  and snapshot_hour > (
      select coalesce(max(snapshot_hour), {% if target.type == 'clickhouse' %}toDateTime('1970-01-01 00:00:00', 'UTC'){% else %}timestamp '1970-01-01 00:00:00 UTC'{% endif %}) from {{ this }}
  )
{% endif %}
