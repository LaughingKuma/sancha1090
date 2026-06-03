{{ config(materialized='table', tags=['adsb']) }}

-- v5.1 seam: LEFT JOIN registry source bronze.aircraft_db and COALESCE(reg.col, bronze) below — a view swap, not a re-ingest.
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
