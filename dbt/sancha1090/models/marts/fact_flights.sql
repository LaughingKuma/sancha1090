{{ config(materialized='table', tags=['flights']) }}

-- Ground-truth flight narratives from OpenSky's flight summaries. A flight surfaces up
-- to 4x (dep+arr lists, D-0+D-2 windows, both tracked endpoints): keep the row with the
-- most airports resolved, then the freshest source ingest (ingested_at is frozen in the
-- source Parquet; committed_at is CH load-time and would re-pick on any reload/retry).
with deduped as (
    select
        *,
        row_number() over (
            partition by icao24, first_seen
            order by
                (case when est_departure_airport is not null then 1 else 0 end)
              + (case when est_arrival_airport is not null then 1 else 0 end) desc,
                ingested_at desc,
                -- deterministic tail: a flight surfaces up to 4x with the same (icao24,first_seen);
                -- order the surfacings so the kept row's direction/window_kind/captured_for_airport is stable.
                window_kind, captured_for_airport, direction,
                est_departure_airport, est_arrival_airport, callsign, last_seen
        ) as rn
    from {{ source('bronze', 'opensky_flights') }}
    where first_seen is not null
),
-- "Seen" = ever surfaced by the Japan-context feed (which covers the antenna's sky too);
-- flags routes as flying over us vs anonymous catalog entries.
seen as (
    select distinct icao24
    from {{ ref('fact_state_snapshots') }}
)
-- Explicit AS icao24: icao24 is ambiguous across deduped(f) and seen(s), and CH keeps the
-- table qualifier in the output column name when unaliased — same fix as fct_flight_legs.
select
    f.icao24 as icao24,
    f.callsign,
    f.first_seen,
    f.last_seen,
    f.flight_duration_seconds,
    f.est_departure_airport as origin_icao,
    {{ ch_blank_null('o.iata') }} as origin_iata,
    {{ ch_blank_null('o.city') }} as origin_city,
    o.name                  as origin_name,
    o.lat                   as origin_lat,
    o.lon                   as origin_lon,
    f.est_arrival_airport   as dest_icao,
    {{ ch_blank_null('d.iata') }} as dest_iata,
    {{ ch_blank_null('d.city') }} as dest_city,
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
