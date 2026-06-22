{{ config(materialized='table', tags=['adsb']) }}

-- Row-count-preserving over bronze: every join is LEFT and every dim/backfill is single-valued per key.
{% if target.type == 'clickhouse' %}
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
        -- JSONExtractInt returns 0 on a missing/absent dbFlags (never NULL), so COALESCE(...,0) below is the
        -- same 2-valued contract as Trino's try_cast(...)->NULL->COALESCE. Spike-proven (mil 0.955% vs 0.953%).
        JSONExtractInt(s._raw_json, 'dbFlags') as db_flags
    from {{ source('bronze', 'adsb_states') }} s
    left join {{ ref('int_adsb_callsign_backfill') }} bf
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
    -- reg_country via the P1 range_hashed dict (replaces the Trino hex BETWEEN range join); macro guards '~' hexes.
    {{ ch_hex_country('b.hex') }} as reg_country
from base b
left join {{ ref('dim_aircraft') }} ac
       on ac.icao24 = lower(b.hex)
left join {{ ref('dim_airlines') }} al
       on al.icao = substring(trimBoth(b.callsign_filled), 1, 3)
      and match(trimBoth(b.callsign_filled), '^[A-Z]{3}[0-9]')  -- guard: skip GA/registration tails like JA45KA
{% else %}
with base as (
    select
        s.capture_ts,
        s.hex,
        s.flight,
        -- Blank ADS-B callsigns (rarer ID message, lost more at range edge) backfilled from the OpenSky
        -- context feed; callsign_source flags provenance so marts can show how much leans on the backfill.
        coalesce(nullif(trim(s.flight), ''), bf.filled_callsign) as callsign_filled,
        case when nullif(trim(s.flight), '') is not null then 'adsb'
             when bf.filled_callsign is not null            then 'opensky_backfill'
        end as callsign_source,
        s.lat,
        s.lon,
        s.alt_baro,
        s.gs,
        s.track,
        db.db_flags
    from {{ source('bronze', 'adsb_states') }} s
    -- Decode seam: swap NULL for the typed bronze column if dbFlags is promoted (v4.x) — no backfill.
    cross join lateral (values (
        coalesce(try_cast(json_extract_scalar(s._raw_json, '$.dbFlags') as integer), null)
    )) db(db_flags)
    -- Single-valued per (hex, capture_ts): the nearest OpenSky callsign within the backfill window.
    left join {{ ref('int_adsb_callsign_backfill') }} bf
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
    -- dbFlags are exception flags: absence means FALSE, not unknown. COALESCE(...,0) keeps these
    -- 2-valued so `= false`, GROUP BY and % math behave (NULL would silently drop ~97% of rows).
    bitwise_and(coalesce(b.db_flags, 0), 1) <> 0 as is_military,
    bitwise_and(coalesce(b.db_flags, 0), 2) <> 0 as is_interesting,
    bitwise_and(coalesce(b.db_flags, 0), 4) <> 0 as is_pia,
    bitwise_and(coalesce(b.db_flags, 0), 8) <> 0 as is_ladd,
    ac.registration,
    ac.typecode,
    ac.category,
    al.name    as airline_name,
    al.country as airline_country,
    ctry.country as reg_country
from base b
left join {{ ref('dim_aircraft') }} ac
       on ac.icao24 = lower(b.hex)
-- Airline of THIS flight, now keyed on the OpenSky-backfilled callsign so blank/edge frames attribute too.
left join {{ ref('dim_airlines') }} al
       on al.icao = substr(trim(b.callsign_filled), 1, 3)
      and regexp_like(trim(b.callsign_filled), '^[A-Z]{3}[0-9]')  -- guard: skip GA/registration tails like JA45KA
left join {{ ref('dim_hex_country') }} ctry
       -- try(): readsb prefixes non-ICAO (TIS-B/ADS-R) addresses with '~'; a bad hex must not abort the build.
       on try(from_base(lower(b.hex), 16)) between ctry.block_lo and ctry.block_hi
{% endif %}
