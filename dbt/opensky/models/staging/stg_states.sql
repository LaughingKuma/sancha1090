with src as (

    select * from {{ source('staging', 'raw_states') }}

),

typed as (

    select
        icao24,
        nullif(trim(callsign), '')              as callsign,
        origin_country,
        to_timestamp(time_position)             as time_position,
        to_timestamp(last_contact)              as last_contact,
        longitude::float                        as longitude,
        latitude::float                         as latitude,
        baro_altitude::float                    as baro_altitude_m,
        on_ground::boolean                      as on_ground,
        velocity::float                         as velocity_mps,
        true_track::float                       as track_deg,
        vertical_rate::float                    as vertical_rate_mps,
        geo_altitude::float                     as geo_altitude_m,
        squawk,
        spi::boolean                            as spi,
        position_source::int                    as position_source,
        to_timestamp(snapshot_time)             as snapshot_time,
        region,
        ingested_at::timestamp                  as ingested_at
    from src

),

dedup as (

    select
        typed.*,
        row_number() over (
            partition by icao24, snapshot_time
            order by ingested_at desc
        ) as rn
    from typed

)

select
    icao24,
    callsign,
    origin_country,
    time_position,
    last_contact,
    longitude,
    latitude,
    baro_altitude_m,
    on_ground,
    velocity_mps,
    track_deg,
    vertical_rate_mps,
    geo_altitude_m,
    squawk,
    spi,
    position_source,
    snapshot_time,
    region,
    ingested_at
from dedup
where rn = 1
