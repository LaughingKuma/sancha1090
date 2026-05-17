with latest_position as (

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
    where on_ground = false
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
from latest_position
group by origin_country
order by airborne_aircraft desc
