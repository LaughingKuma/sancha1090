{{ config(materialized='table') }}

with latest_ts as (
    select max(snapshot_time) as ts from {{ ref('stg_states') }}
),
recent as (
    select *
    from {{ ref('stg_states') }}
    where snapshot_time >= (select ts - {% if target.type == 'clickhouse' %}INTERVAL 5 MINUTE{% else %}interval '5' minute{% endif %} from latest_ts)
      and on_ground = false
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
    origin_country,
    count(*)                                            as airborne_aircraft,
    cast(avg(velocity_mps * 3.6) as decimal(10, 2))     as avg_speed_kmh,
    cast(avg(baro_altitude_m)    as decimal(10, 2))     as avg_altitude_m,
    max(snapshot_time)                                  as snapshot_ts
from current_state
group by origin_country
order by airborne_aircraft desc
