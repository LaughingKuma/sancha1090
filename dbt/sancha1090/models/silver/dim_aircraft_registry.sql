{{ config(materialized='table', tags=['flights']) }}

-- One row per airframe from the weekly registry snapshots, latest as_of_date wins.
-- Country comes from the ICAO24 address block (dim_hex_country) — the registry CSV
-- subset carries no country column, and the block is authoritative anyway.
with latest as (
    select
        *,
        row_number() over (
            partition by icao24
            order by as_of_date desc, committed_at desc
        ) as rn
    from {{ source('bronze', 'aircraft_db') }}
)
select
    l.icao24,
    l.registration,
    l.manufacturername as manufacturer,
    l.model,
    l.typecode,
    l.icaoaircrafttype,
    l.operator,
    l.operatorcallsign,
    l.operatoricao,
    l.owner,
    {{ ch_hex_country('l.icao24') }} as country_of_registration,
    l.as_of_date
from latest l
where l.rn = 1
