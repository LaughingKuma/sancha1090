{{ config(materialized='table', tags=['adsb']) }}

-- Row-count-preserving over bronze: every join is LEFT and every dim/backfill is single-valued per key.
with base as (
    select
        s.capture_ts,
        s.hex,
        s.flight,
        -- Blank ADS-B callsigns (rarer ID message, lost more at range edge) backfilled from the OpenSky feed.
        coalesce(nullIf(trimBoth(s.flight), ''), bf.filled_callsign) as callsign_filled,
        case when nullIf(trimBoth(s.flight), '') is not null then 'adsb'
             when bf.filled_callsign is not null               then 'opensky_backfill'
        end as callsign_source,
        s.lat,
        s.lon,
        s.alt_baro,
        s.gs,
        s.track,
        -- db_flags is the dbFlags integer baked at load (v6.3 eliminated _raw_json from CH); 0 on absent (the
        -- same 2-valued contract JSONExtractInt gave), so the COALESCE(...,0) bit-tests below stay correct.
        s.db_flags as db_flags
    from {{ source('bronze', 'adsb_states') }} s
    left join {{ ref('int_adsb_callsign_from_opensky') }} bf
           on bf.hex = s.hex and bf.capture_ts = s.capture_ts
)
select
    b.capture_ts,
    b.hex,
    b.flight,
    b.callsign_filled,
    b.callsign_source,
    b.lat,
    b.lon,
    b.alt_baro,
    b.gs,
    b.track,
    bitAnd(coalesce(b.db_flags, 0), 1) != 0 as is_military,
    bitAnd(coalesce(b.db_flags, 0), 2) != 0 as is_interesting,
    bitAnd(coalesce(b.db_flags, 0), 4) != 0 as is_pia,
    bitAnd(coalesce(b.db_flags, 0), 8) != 0 as is_ladd,
    ac.registration,
    ac.typecode,
    ac.category,
    al.name    as airline_name,
    al.country as airline_country,
    -- reg_country via the P1 range_hashed dict; macro guards '~' hexes.
    {{ ch_hex_country('b.hex') }} as reg_country
from base b
left join {{ ref('dim_aircraft') }} ac
       on ac.icao24 = lower(b.hex)
left join {{ ref('dim_airlines') }} al
       on al.icao = substring(trimBoth(b.callsign_filled), 1, 3)
      and match(trimBoth(b.callsign_filled), '^[A-Z]{3}[0-9]')  -- guard: skip GA/registration tails like JA45KA
