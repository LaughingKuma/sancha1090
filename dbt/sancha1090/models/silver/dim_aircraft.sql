{{ config(materialized='table', tags=['adsb']) }}

-- v5.1: the registry seam is live — dim_aircraft_registry (tag:flights) must exist before
-- this builds; on a FRESH deploy run ingest_aircraft_db + transform_flights once first.
with seen as (
    select
        lower(hex) as icao24,
        -- filter(non-null): a null snapshot must not blank an already-known static field.
        max_by(r, capture_ts) filter (where r is not null)              as registration,
        max_by(t, capture_ts) filter (where t is not null)              as typecode,
        max_by("desc", capture_ts) filter (where "desc" is not null)    as aircraft_desc,
        max_by(category, capture_ts) filter (where category is not null) as category,
        -- sparse (~18%) and NOT the airline source — operating airline flows via callsign -> dim_airlines.
        max_by(ownop, capture_ts) filter (where ownop is not null)      as operator_raw
    from {{ source('bronze', 'adsb_states') }}
    where hex is not null
    group by lower(hex)
)
-- Registry wins where present (authoritative identity); decoded ADS-B fields fill the rest.
select
    s.icao24,
    coalesce(reg.registration, s.registration) as registration,
    coalesce(reg.typecode, s.typecode)         as typecode,
    s.aircraft_desc,
    s.category,
    s.operator_raw,
    reg.operator                               as operator,
    reg.owner                                  as owner,
    reg.model                                  as model,
    reg.manufacturer                           as manufacturer,
    reg.country_of_registration                as country_of_registration
from seen s
left join {{ ref('dim_aircraft_registry') }} reg
    on reg.icao24 = s.icao24
