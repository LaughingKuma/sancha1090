{{ config(materialized='table', tags=['adsb']) }}

-- v5.1: the registry seam is live — dim_aircraft_registry (tag:flights) must exist before
-- this builds; on a FRESH deploy run ingest_aircraft_db + transform_flights once first.
with seen as (
    -- argMaxIf(arg, capture_ts, arg IS NOT NULL) keeps the latest non-null value per field.
    -- bronze is case-faithful to the edge schema: `desc` is reserved (backtick), ownOp is camelCase.
    select
        lower(hex) as icao24,
        argMaxIf(r, capture_ts, r IS NOT NULL)                as registration,
        argMaxIf(t, capture_ts, t IS NOT NULL)                as typecode,
        argMaxIf(`desc`, capture_ts, `desc` IS NOT NULL)      as aircraft_desc,
        argMaxIf(category, capture_ts, category IS NOT NULL)  as category,
        argMaxIf(ownOp, capture_ts, ownOp IS NOT NULL)        as operator_raw
    from {{ source('bronze', 'adsb_states') }}
    where hex is not null
    group by lower(hex)
)
-- Registry wins where present (authoritative identity); decoded ADS-B fields fill the rest.
select
    s.icao24 as icao24,
    coalesce(reg.registration, s.registration) as registration,
    coalesce(reg.typecode, s.typecode)         as typecode,
    s.aircraft_desc,
    s.category,
    s.operator_raw,
    reg.operator                               as operator,
    reg.owner                                  as owner,
    -- Registry model is sparse (~15% blank); fall back to the ICAO type designator's
    -- generic model name so a decoded typecode still yields a human-readable model.
    coalesce(reg.model, act.model_name)        as model,
    reg.manufacturer                           as manufacturer,
    -- Country is fixed by the ICAO24 address block, so derive it from the hex directly
    -- (identical to the registry's own computation) — covers airframes absent from the registry.
    {{ ch_hex_country('s.icao24') }}           as country_of_registration
from seen s
left join {{ ref('dim_aircraft_registry') }} reg
    on reg.icao24 = s.icao24
left join {{ ref('dim_aircraft_types') }} act
    on act.typecode = coalesce(reg.typecode, s.typecode)
