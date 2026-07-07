-- Snap-only OpenSky-states inferred provenance view: consumes int_flight_legs_opensky (sessionize +
-- sched-gated snap); endpoint resolution beyond snap now lives in fct_flights_reconciled. Untagged.
{{ config(materialized='table') }}

with antenna as (
    -- Rooftop is the only military signal; left-joined so states legs without it read false.
    select hex, max(is_military) as is_military
    from {{ ref('fct_adsb_state') }}
    group by hex
)
select
    l.icao24 as icao24,
    l.leg_id as leg_id,
    trimBoth(l.callsign) as callsign,
    l.start_time,
    l.end_time,
    intDiv(dateDiff('second', l.start_time, l.end_time), 60) as duration_min,
    l.num_fixes,
    l.first_lat, l.first_lon, l.first_alt_m,
    l.last_lat,  l.last_lon,  l.last_alt_m,
    l.origin_icao, l.origin_name, l.origin_lat, l.origin_lon,
    l.dest_icao,   l.dest_name,   l.dest_lat,   l.dest_lon,
    if(l.origin_icao is not null, 'snap', null) as origin_source,
    if(l.dest_icao   is not null, 'snap', null) as dest_source,
    ac.registration,
    ac.typecode,
    al.name    as airline_name,
    al.country as airline_country,
    {{ ch_hex_country('l.icao24') }} as reg_country,
    coalesce(ant.is_military, false) as is_military,
    ant.hex is not null              as crossed_antenna,
    case when l.origin_icao is not null and l.dest_icao is not null
         then concat(l.origin_icao, '-', l.dest_icao) end as route_inferred,
    -- snap is the only source now; the label stays for schema stability (per-endpoint truth = origin_source/dest_source).
    if(l.origin_icao is not null or l.dest_icao is not null, 'snap', null) as route_source
from {{ ref('int_flight_legs_opensky') }} l
left join {{ ref('dim_aircraft') }} ac on ac.icao24 = lower(l.icao24)
left join {{ ref('dim_airlines') }} al
       on al.icao = substring(trimBoth(l.callsign), 1, 3)
      and match(trimBoth(l.callsign), '^[A-Z]{3}[0-9]')
left join antenna ant on ant.hex = lower(l.icao24)
