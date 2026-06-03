{{ config(materialized='table', tags=['adsb']) }}

-- Row-count-preserving over bronze: every join is LEFT and every dim is single-valued per key.
select
    s.capture_ts,
    s.hex,
    s.flight,
    s.lat,
    s.lon,
    s.alt_baro,
    s.gs,
    s.track,
    -- dbFlags are exception flags: absence means FALSE, not unknown. COALESCE(...,0) keeps these
    -- 2-valued so `= false`, GROUP BY and % math behave (NULL would silently drop ~97% of rows).
    bitwise_and(coalesce(db.db_flags, 0), 1) <> 0 as is_military,
    bitwise_and(coalesce(db.db_flags, 0), 2) <> 0 as is_interesting,
    bitwise_and(coalesce(db.db_flags, 0), 4) <> 0 as is_pia,
    bitwise_and(coalesce(db.db_flags, 0), 8) <> 0 as is_ladd,
    ac.registration,
    ac.typecode,
    ac.category,
    al.name    as airline_name,
    al.country as airline_country,
    ctry.country as reg_country
from {{ source('bronze', 'adsb_states') }} s
-- Decode seam: swap NULL for the typed bronze column if dbFlags is promoted (v4.x) — no backfill.
cross join lateral (values (
    coalesce(try_cast(json_extract_scalar(s._raw_json, '$.dbFlags') as integer), null)
)) db(db_flags)
left join {{ ref('dim_aircraft') }} ac
       on ac.icao24 = lower(s.hex)
-- Airline of THIS flight (callsign), a different question than the airframe owner (leasing/codeshare).
left join {{ ref('dim_airlines') }} al
       on al.icao = substr(trim(s.flight), 1, 3)
      and regexp_like(trim(s.flight), '^[A-Z]{3}[0-9]')  -- guard: skip GA/registration tails like JA45KA
left join {{ ref('dim_hex_country') }} ctry
       -- try(): readsb prefixes non-ICAO (TIS-B/ADS-R) addresses with '~'; a bad hex must not abort the build.
       on try(from_base(lower(s.hex), 16)) between ctry.block_lo and ctry.block_hi
