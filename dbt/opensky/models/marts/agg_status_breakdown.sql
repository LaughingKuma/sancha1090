{{ config(materialized='table') }}

-- Latest snapshot (5-minute window catches all 8 regions of one run).
-- Airborne vs on-ground breakdown for the current moment.

with latest_ts as (
    select max(snapshot_time) as ts from {{ ref('stg_states') }}
),
recent as (
    select *
    from {{ ref('stg_states') }}
    where snapshot_time >= (select ts - interval '5' minute from latest_ts)
      and latitude is not null
      and longitude is not null
),
ranked as (
    select recent.*,
           row_number() over (partition by icao24 order by snapshot_time desc) as rn
    from recent
),
current_state as (
    select * from ranked where rn = 1
)
select
    case when on_ground then 'on_ground' else 'airborne' end as status,
    count(*)               as aircraft_count,
    max(snapshot_time)     as snapshot_ts
from current_state
group by 1
order by 2 desc
