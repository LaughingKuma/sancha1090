{{ config(materialized='table', tags=['flights']) }}

-- Deploy-order guard: bronze.adsbx_aircraft_db is created by the operator's manual DDL at deploy, but
-- transform_flights rebuilds this model from committed code — gate the ADSBx fill on the table existing.
{%- if execute %}
{%- set adsbx_rel = adapter.get_relation(database=none, schema='bronze', identifier='adsbx_aircraft_db') %}
{%- else %}
{%- set adsbx_rel = none %}
{%- endif %}

-- One row per airframe from the weekly registry snapshots, latest as_of_date wins.
-- Country comes from the ICAO24 address block (dim_hex_country) — the registry CSV
-- subset carries no country column, and the block is authoritative anyway.
with latest as (
    select
        *,
        -- Content-total order: dupe-icao24 rows exist within one snapshot (8 live), and a
        -- non-total tiebreak picks an arbitrary identity per rebuild (house determinism rule).
        row_number() over (
            partition by icao24
            order by as_of_date desc, committed_at desc,
                     registration, typecode, icaoaircrafttype, manufacturername, model,
                     operator, operatorcallsign, operatoricao, owner
        ) as rn
    from {{ source('bronze', 'aircraft_db') }}
),
reg as (select * from latest where rn = 1)
{%- if adsbx_rel is not none %},
adsbx_latest as (
    select
        *,
        row_number() over (
            partition by icao24
            order by as_of_date desc, committed_at desc,
                     registration, icaotype, short_type, manufacturer, model, ownop,
                     year, faa_pia, faa_ladd, mil
        ) as rn
    from {{ source('bronze', 'adsbx_aircraft_db') }}
),
-- ADSBx fills registry blanks per column (typecode is the payload — it closes the jet-gate blind spot:
-- 843 typeless hexes / 17.8k flights measured 2026-07-21). ownop is the FAA registrant -> owner only.
adsbx as (select * from adsbx_latest where rn = 1)
{%- endif %}
{%- if adsbx_rel is not none %}
select
    coalesce(l.icao24, a.icao24)                              as icao24,
    coalesce(nullIf(l.registration, ''), a.registration)      as registration,
    coalesce(nullIf(l.manufacturername, ''), a.manufacturer)  as manufacturer,
    coalesce(nullIf(l.model, ''), a.model)                    as model,
    coalesce(nullIf(l.typecode, ''), a.icaotype)              as typecode,
    l.icaoaircrafttype                                        as icaoaircrafttype,
    l.operator                                                as operator,
    l.operatorcallsign                                        as operatorcallsign,
    l.operatoricao                                            as operatoricao,
    coalesce(nullIf(l.owner, ''), a.ownop)                    as owner,
    {{ ch_hex_country('coalesce(l.icao24, a.icao24)') }}      as country_of_registration,
    coalesce(l.as_of_date, a.as_of_date)                      as as_of_date
from reg l
full outer join adsbx a on a.icao24 = l.icao24
{%- else %}
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
from reg l
{%- endif %}
