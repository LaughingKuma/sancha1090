{{ config(materialized='table') }}

{% if target.type == 'trino' %}

with latest_ts as (
    select max(snapshot_time) as ts from {{ ref('stg_states') }}
),
recent as (
    select *
    from {{ ref('stg_states') }}
    where snapshot_time >= (select ts - interval '5' minute from latest_ts)
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

{% else %}

with latest_ts as (
    select max(snapshot_time) as ts from {{ ref('stg_states') }}
),
current_state as (
    select distinct on (icao24)
        icao24,
        callsign,
        origin_country,
        latitude,
        longitude,
        baro_altitude_m,
        velocity_mps,
        on_ground,
        snapshot_time
    from {{ ref('stg_states') }}
    where snapshot_time >= (select ts - interval '5 minutes' from latest_ts)
      and on_ground = false
      and latitude is not null
      and longitude is not null
    order by icao24, snapshot_time desc
)
select
    origin_country,
    count(*)                                    as airborne_aircraft,
    avg(velocity_mps * 3.6)::numeric(10, 2)     as avg_speed_kmh,
    avg(baro_altitude_m)::numeric(10, 2)        as avg_altitude_m,
    max(snapshot_time)                          as snapshot_ts
from current_state
group by origin_country
order by airborne_aircraft desc

{% endif %}
