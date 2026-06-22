-- Reads the OpenSky context feed (fact_state_snapshots), so it's built by transform_marts (untagged),
-- not the rooftop-triggered transform_adsb_silver — keeps it fresh on OpenSky context ticks.
-- BOOTSTRAP: still refs tag:adsb relations built by transform_adsb_silver (dim_airports/dim_airlines/
-- dim_hex_country seeds, dim_aircraft, fct_adsb_state). Steady state is fine; on a FRESH deploy run
-- transform_adsb_silver once before transform_marts or this errors on a missing relation.
{{ config(
    materialized='table',
    query_settings=(
        {'max_bytes_before_external_group_by': 30000000, 'max_bytes_before_external_sort': 30000000}
        if target.type == 'clickhouse' else none
    )
) }}

-- INFERRED legs from the ~12-min OpenSky context feed: sessionize each airframe, snap low-altitude
-- endpoints to airports. route_inferred is an approximation, NOT ground truth (fact_flights is authoritative).
{% if target.type == 'clickhouse' %}
-- CH lane (P3b): lagInFrame+framed cumsum sessionization (spill-safe, query_settings above), non-equi
-- airport snap as a lat-bucket equi-join, reg_country via the P1 range_hashed dict. Spike-proven exact.
with ordered as (
    select
        icao24,
        snapshot_time,
        latitude,
        longitude,
        baro_altitude_m,
        on_ground,
        callsign,
        -- toNullable+NULL default so the first row of each airframe reads prev_time NULL, matching Trino lag()
        -- (bare lagInFrame fills the type default, not NULL); explicit ROWS frame = the immediately-prior row.
        lagInFrame(toNullable(snapshot_time), 1, NULL)
            over (partition by icao24 order by snapshot_time rows between 1 preceding and current row) as prev_time,
        lagInFrame(toNullable(on_ground), 1, NULL)
            over (partition by icao24 order by snapshot_time rows between 1 preceding and current row) as prev_on_ground
    from {{ ref('fact_state_snapshots') }}
),
flagged as (
    select *,
        case
            when prev_time is null then 1
            when dateDiff('second', prev_time, snapshot_time) > {{ var('legs_gap_min') }} * 60 then 1
            when on_ground and not prev_on_ground then 1  -- ground-contact flip: prior flight landed, split here
            else 0
        end as leg_break
    from ordered
),
legged as (
    select *,
        sum(leg_break) over (
            partition by icao24 order by snapshot_time
            rows between unbounded preceding and current row
        ) as leg_id
    from flagged
),
airborne as (
    -- Leg geometry/endpoints use airborne fixes only; all-ground legs (parked) drop out at the group-by.
    select * from legged where not on_ground
),
callsign_choice as (
    -- Deterministic dominant callsign per leg: most frequent, ties broken by earliest-seen then lexical.
    select icao24, leg_id, callsign
    from (
        select icao24, leg_id, callsign,
               row_number() over (
                   partition by icao24, leg_id
                   order by cs_cnt desc, first_seen asc, callsign asc
               ) as rn
        from (
            select icao24, leg_id, callsign,
                   count(*)           as cs_cnt,
                   min(snapshot_time) as first_seen
            from airborne
            where callsign is not null
            group by icao24, leg_id, callsign
        )
    )
    where rn = 1
),
legs as (
    select
        icao24,
        leg_id,
        min(snapshot_time) as start_time,
        max(snapshot_time) as end_time,
        -- intDiv(dateDiff('second'),60), NOT dateDiff('minute'): CH counts minute-boundary crossings, Trino truncates.
        intDiv(dateDiff('second', min(snapshot_time), max(snapshot_time)), 60) as duration_min,
        count(*) as num_fixes,
        argMin(latitude, snapshot_time)        as first_lat,
        argMin(longitude, snapshot_time)       as first_lon,
        -- tuple() keeps a NULL endpoint altitude: bare argMin SKIPS NULL args and would fall through to a
        -- later non-null fix, but Trino min_by/max_by returns the endpoint's actual NULL. A NULL first_alt_m
        -- must fail `< legs_cruise_alt_m` so the endpoint stays unsnapped (origin/dest NULL), matching Trino.
        argMin(tuple(baro_altitude_m), snapshot_time).1 as first_alt_m,
        argMax(latitude, snapshot_time)        as last_lat,
        argMax(longitude, snapshot_time)       as last_lon,
        argMax(tuple(baro_altitude_m), snapshot_time).1 as last_alt_m
    from airborne
    group by icao24, leg_id
),
origin_snap as (
    -- Lat-bucket equi-join replaces Trino's non-equi haversine join: arrayJoin each low-alt origin into 3
    -- floor(lat)±1 buckets, equi-join dim_airports on the bucket, then the exact lat/lon/haversine residual.
    select l.icao24, l.leg_id,
           a.icao as origin_icao, a.name as origin_name, a.lat as origin_lat, a.lon as origin_lon,
           row_number() over (partition by l.icao24, l.leg_id
                              order by {{ haversine_km('l.first_lat', 'l.first_lon', 'a.lat', 'a.lon') }}) as rn
    from (
        select icao24, leg_id, first_lat, first_lon, first_alt_m,
               arrayJoin([toInt32(floor(first_lat)) - 1, toInt32(floor(first_lat)), toInt32(floor(first_lat)) + 1]) as lat_bucket
        from legs
        where first_alt_m < {{ var('legs_cruise_alt_m') }}
    ) l
    join (select icao, name, lat, lon, toInt32(floor(lat)) as lat_bucket from {{ ref('dim_airports') }}) a
      on a.lat_bucket = l.lat_bucket
    where a.lat between l.first_lat - {{ var('legs_snap_km') }} / 110.574 and l.first_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - l.first_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(l.first_lat)), 0.01))
      and {{ haversine_km('l.first_lat', 'l.first_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
),
dest_snap as (
    select l.icao24, l.leg_id,
           a.icao as dest_icao, a.name as dest_name, a.lat as dest_lat, a.lon as dest_lon,
           row_number() over (partition by l.icao24, l.leg_id
                              order by {{ haversine_km('l.last_lat', 'l.last_lon', 'a.lat', 'a.lon') }}) as rn
    from (
        select icao24, leg_id, last_lat, last_lon, last_alt_m,
               arrayJoin([toInt32(floor(last_lat)) - 1, toInt32(floor(last_lat)), toInt32(floor(last_lat)) + 1]) as lat_bucket
        from legs
        where last_alt_m < {{ var('legs_cruise_alt_m') }}
    ) l
    join (select icao, name, lat, lon, toInt32(floor(lat)) as lat_bucket from {{ ref('dim_airports') }}) a
      on a.lat_bucket = l.lat_bucket
    where a.lat between l.last_lat - {{ var('legs_snap_km') }} / 110.574 and l.last_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - l.last_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(l.last_lat)), 0.01))
      and {{ haversine_km('l.last_lat', 'l.last_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
),
antenna as (
    -- Rooftop is the only military signal; left-joined below so OpenSky context legs without it read false.
    -- bool_or -> max over the UInt8 is_military (CH has no bool_or); reads the P3a silver_ch.fct_adsb_state.
    select hex, max(is_military) as is_military
    from {{ ref('fct_adsb_state') }}
    group by hex
)
select
    -- CH keeps the table qualifier in the output column name for `alias.col` (icao24/leg_id/callsign appear
    -- across the joined CTEs), so alias them explicitly to match Trino's bare names (downstream + parity).
    l.icao24 as icao24,
    l.leg_id as leg_id,
    cc.callsign as callsign,
    l.start_time,
    l.end_time,
    l.duration_min,
    l.num_fixes,
    l.first_lat, l.first_lon, l.first_alt_m,
    l.last_lat,  l.last_lon,  l.last_alt_m,
    o.origin_icao, o.origin_name, o.origin_lat, o.origin_lon,
    d.dest_icao,   d.dest_name,   d.dest_lat,   d.dest_lon,
    case when o.origin_icao is not null and d.dest_icao is not null
         then concat(o.origin_icao, '-', d.dest_icao) end as route_inferred,
    ac.registration,
    ac.typecode,
    al.name    as airline_name,
    al.country as airline_country,
    -- reg_country via the P1 range_hashed dict (replaces the Trino hex BETWEEN range join); macro guards '~' hexes.
    {{ ch_hex_country('l.icao24') }} as reg_country,
    coalesce(ant.is_military, false) as is_military,
    ant.hex is not null              as crossed_antenna
from legs l
left join callsign_choice cc on cc.icao24 = l.icao24 and cc.leg_id = l.leg_id
left join (select * from origin_snap where rn = 1) o on o.icao24 = l.icao24 and o.leg_id = l.leg_id
left join (select * from dest_snap   where rn = 1) d on d.icao24 = l.icao24 and d.leg_id = l.leg_id
left join {{ ref('dim_aircraft') }} ac on ac.icao24 = lower(l.icao24)
-- Operating airline of THIS leg via callsign; same GA-tail regex guard as silver.
left join {{ ref('dim_airlines') }} al
       on al.icao = substring(trimBoth(cc.callsign), 1, 3)
      and match(trimBoth(cc.callsign), '^[A-Z]{3}[0-9]')
left join antenna ant on ant.hex = lower(l.icao24)
{% else %}
with ordered as (
    select
        icao24,
        snapshot_time,
        latitude,
        longitude,
        baro_altitude_m,
        on_ground,
        callsign,
        lag(snapshot_time) over w as prev_time,
        lag(on_ground)     over w as prev_on_ground
    from {{ ref('fact_state_snapshots') }}
    window w as (partition by icao24 order by snapshot_time)
),
flagged as (
    select *,
        case
            when prev_time is null then 1
            when date_diff('second', prev_time, snapshot_time) > {{ var('legs_gap_min') }} * 60 then 1
            when on_ground and not prev_on_ground then 1  -- ground-contact flip: prior flight landed, split here
            else 0
        end as leg_break
    from ordered
),
legged as (
    select *,
        sum(leg_break) over (
            partition by icao24 order by snapshot_time
            rows between unbounded preceding and current row
        ) as leg_id
    from flagged
),
airborne as (
    -- Leg geometry/endpoints use airborne fixes only; all-ground legs (parked) drop out at the group-by.
    select * from legged where not on_ground
),
callsign_choice as (
    -- Deterministic dominant callsign per leg: most frequent, ties broken by earliest-seen then lexical.
    -- (max_by(callsign, count) would break count ties arbitrarily -> non-reproducible airline_name; ~4.5% of legs tie.)
    select icao24, leg_id, callsign
    from (
        select icao24, leg_id, callsign,
               row_number() over (
                   partition by icao24, leg_id
                   order by cs_cnt desc, first_seen asc, callsign asc
               ) as rn
        from (
            select icao24, leg_id, callsign,
                   count(*)           as cs_cnt,
                   min(snapshot_time) as first_seen
            from airborne
            where callsign is not null
            group by icao24, leg_id, callsign
        )
    )
    where rn = 1
),
legs as (
    select
        icao24,
        leg_id,
        min(snapshot_time) as start_time,
        max(snapshot_time) as end_time,
        date_diff('minute', min(snapshot_time), max(snapshot_time)) as duration_min,
        count(*) as num_fixes,
        min_by(latitude, snapshot_time)        as first_lat,
        min_by(longitude, snapshot_time)       as first_lon,
        min_by(baro_altitude_m, snapshot_time) as first_alt_m,
        max_by(latitude, snapshot_time)        as last_lat,
        max_by(longitude, snapshot_time)       as last_lon,
        max_by(baro_altitude_m, snapshot_time) as last_alt_m
    from airborne
    group by icao24, leg_id
),
origin_snap as (
    select l.icao24, l.leg_id,
           a.icao as origin_icao, a.name as origin_name, a.lat as origin_lat, a.lon as origin_lon,
           row_number() over (partition by l.icao24, l.leg_id
                              order by {{ haversine_km('l.first_lat', 'l.first_lon', 'a.lat', 'a.lon') }}) as rn
    from legs l
    join {{ ref('dim_airports') }} a
      on l.first_alt_m < {{ var('legs_cruise_alt_m') }}
     -- cheap prefilter before haversine; lon window scales with latitude (cos) and wraps the antimeridian.
     and a.lat between l.first_lat - {{ var('legs_snap_km') }} / 110.574 and l.first_lat + {{ var('legs_snap_km') }} / 110.574
     and abs(mod(a.lon - l.first_lon + 540, 360) - 180)
           <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(l.first_lat)), 0.01))
     and {{ haversine_km('l.first_lat', 'l.first_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
),
dest_snap as (
    select l.icao24, l.leg_id,
           a.icao as dest_icao, a.name as dest_name, a.lat as dest_lat, a.lon as dest_lon,
           row_number() over (partition by l.icao24, l.leg_id
                              order by {{ haversine_km('l.last_lat', 'l.last_lon', 'a.lat', 'a.lon') }}) as rn
    from legs l
    join {{ ref('dim_airports') }} a
      on l.last_alt_m < {{ var('legs_cruise_alt_m') }}
     and a.lat between l.last_lat - {{ var('legs_snap_km') }} / 110.574 and l.last_lat + {{ var('legs_snap_km') }} / 110.574
     and abs(mod(a.lon - l.last_lon + 540, 360) - 180)
           <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(l.last_lat)), 0.01))
     and {{ haversine_km('l.last_lat', 'l.last_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
),
antenna as (
    -- Rooftop is the only military signal; left-joined below so OpenSky context legs without it simply read false.
    select hex, bool_or(is_military) as is_military
    from {{ ref('fct_adsb_state') }}
    group by hex
)
select
    l.icao24,
    l.leg_id,
    cc.callsign,
    l.start_time,
    l.end_time,
    l.duration_min,
    l.num_fixes,
    l.first_lat, l.first_lon, l.first_alt_m,
    l.last_lat,  l.last_lon,  l.last_alt_m,
    o.origin_icao, o.origin_name, o.origin_lat, o.origin_lon,
    d.dest_icao,   d.dest_name,   d.dest_lat,   d.dest_lon,
    case when o.origin_icao is not null and d.dest_icao is not null
         then o.origin_icao || '-' || d.dest_icao end as route_inferred,
    ac.registration,
    ac.typecode,
    al.name    as airline_name,
    al.country as airline_country,
    ctry.country as reg_country,
    coalesce(ant.is_military, false) as is_military,
    ant.hex is not null              as crossed_antenna
from legs l
left join callsign_choice cc on cc.icao24 = l.icao24 and cc.leg_id = l.leg_id
left join (select * from origin_snap where rn = 1) o on o.icao24 = l.icao24 and o.leg_id = l.leg_id
left join (select * from dest_snap   where rn = 1) d on d.icao24 = l.icao24 and d.leg_id = l.leg_id
left join {{ ref('dim_aircraft') }} ac on ac.icao24 = lower(l.icao24)
-- Operating airline of THIS leg via callsign; same GA-tail regex guard as silver.
left join {{ ref('dim_airlines') }} al
       on al.icao = substr(trim(cc.callsign), 1, 3)
      and regexp_like(trim(cc.callsign), '^[A-Z]{3}[0-9]')
left join {{ ref('dim_hex_country') }} ctry
       on try(from_base(lower(l.icao24), 16)) between ctry.block_lo and ctry.block_hi
left join antenna ant on ant.hex = lower(l.icao24)
{% endif %}
