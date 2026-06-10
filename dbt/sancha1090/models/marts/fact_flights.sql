{{ config(materialized='table', tags=['flights']) }}

-- Ground-truth flight narratives from OpenSky's flight summaries. A flight surfaces up
-- to 4x (dep+arr lists, D-0+D-2 windows, both tracked endpoints): keep the row with the
-- most airports resolved, then the freshest commit.
with deduped as (
    select
        *,
        row_number() over (
            partition by icao24, first_seen
            order by
                (case when est_departure_airport is not null then 1 else 0 end)
              + (case when est_arrival_airport is not null then 1 else 0 end) desc,
                committed_at desc
        ) as rn
    from {{ source('bronze', 'opensky_flights') }}
),
-- "Seen" = surfaced by the Japan-context feed (which covers the antenna's sky too);
-- marts filter on this so routes are the ones flying over us, not anonymous catalogs.
seen as (
    select distinct icao24
    from {{ ref('fact_state_snapshots') }}
    where snapshot_time > current_timestamp - interval '30' day
)
select
    f.icao24,
    f.callsign,
    f.first_seen,
    f.last_seen,
    f.flight_duration_seconds,
    f.est_departure_airport as origin_icao,
    o.iata                  as origin_iata,
    o.city                  as origin_city,
    o.name                  as origin_name,
    o.lat                   as origin_lat,
    o.lon                   as origin_lon,
    f.est_arrival_airport   as dest_icao,
    d.iata                  as dest_iata,
    d.city                  as dest_city,
    d.name                  as dest_name,
    d.lat                   as dest_lat,
    d.lon                   as dest_lon,
    reg.registration,
    reg.typecode,
    reg.model,
    reg.operator,
    reg.operatoricao,
    reg.owner,
    reg.country_of_registration,
    (s.icao24 is not null)  as seen_in_context,
    f.captured_for_airport,
    f.direction,
    f.window_kind,
    f.committed_at
from deduped f
left join {{ ref('dim_airports') }} o on o.icao = f.est_departure_airport
left join {{ ref('dim_airports') }} d on d.icao = f.est_arrival_airport
left join {{ ref('dim_aircraft_registry') }} reg on reg.icao24 = f.icao24
left join seen s on s.icao24 = f.icao24
where f.rn = 1
