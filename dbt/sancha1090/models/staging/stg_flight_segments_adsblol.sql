{{ config(materialized='table', tags=['adsblol']) }}

-- Global flight segments from adsb.lol full traces (route backstory lane).
-- FINAL: bronze is RMT versioned on ingested_at — a refetched trace must read once.
-- ft->m here so downstream speaks legs_cruise_alt_m's unit.
select
    lower(icao24)                        as icao24,
    nullIf(trimBoth(callsign), '')       as callsign,
    seg_start                            as seg_start_time,
    seg_end                              as seg_end_time,
    num_fixes,
    first_lat, first_lon,
    first_alt_ft * 0.3048                as first_alt_m,
    coalesce(first_on_ground, false)     as first_on_ground,
    last_lat, last_lon,
    last_alt_ft * 0.3048                 as last_alt_m,
    coalesce(last_on_ground, false)      as last_on_ground,
    trace_day
from {{ source('bronze', 'adsblol_flight_segments') }} final
