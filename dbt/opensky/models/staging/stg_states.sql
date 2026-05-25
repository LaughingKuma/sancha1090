{% if target.type == 'trino' %}

-- Trino reads the Polaris-backed bronze Iceberg table directly; columns are
-- already typed, so no ::casts. 30-day filter mirrors the Postgres mirror's
-- retention trim — without it the mart rebuild would scan all bronze history.
with src as (
    select *
    from {{ source('bronze', 'opensky_states') }}
    where snapshot_time >= current_timestamp - interval '30' day
),
typed as (
    select
        icao24,
        nullif(trim(callsign), '')      as callsign,
        origin_country,
        time_position,
        last_contact,
        longitude                       as longitude,
        latitude                        as latitude,
        baro_altitude                   as baro_altitude_m,
        on_ground                       as on_ground,
        velocity                        as velocity_mps,
        true_track                      as track_deg,
        vertical_rate                   as vertical_rate_mps,
        geo_altitude                    as geo_altitude_m,
        squawk,
        spi                             as spi,
        position_source                 as position_source,
        snapshot_time,
        region,
        ingested_at
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
    icao24, callsign, origin_country, time_position, last_contact,
    longitude, latitude, baro_altitude_m, on_ground, velocity_mps,
    track_deg, vertical_rate_mps, geo_altitude_m, squawk, spi,
    position_source, snapshot_time, region, ingested_at
from dedup
where rn = 1

{% else %}

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

{% endif %}
