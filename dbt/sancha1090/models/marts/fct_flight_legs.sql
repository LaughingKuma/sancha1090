-- Reads the OpenSky context feed (fact_state_snapshots), so it's built by transform_marts (untagged),
-- not the rooftop-triggered transform_adsb_silver — keeps it fresh on OpenSky context ticks.
-- BOOTSTRAP: still refs tag:adsb relations built by transform_adsb_silver (dim_airports/dim_airlines/
-- dim_hex_country seeds, dim_aircraft, fct_adsb_state) AND the dim_route_overrides seed (loaded only by
-- a manual `dbt seed`). On a FRESH deploy run transform_adsb_silver + seed before transform_marts.
{{ config(
    materialized='table',
    query_settings={'max_bytes_before_external_group_by': 30000000, 'max_bytes_before_external_sort': 30000000}
) }}

-- INFERRED legs from the ~12-min OpenSky context feed: sessionize each airframe, snap low-altitude
-- endpoints to airports. route_inferred is an approximation, NOT ground truth (fact_flights is authoritative).
-- lagInFrame+framed cumsum sessionization (spill-safe, query_settings above), airport snap as a
-- lat-bucket equi-join, reg_country via the P1 range_hashed dict.
with ordered as (
    select
        icao24,
        snapshot_time,
        latitude,
        longitude,
        baro_altitude_m,
        on_ground,
        callsign,
        -- toNullable+NULL default so the first row of each airframe reads prev_time NULL (bare lagInFrame
        -- fills the type default, not NULL); explicit ROWS frame = the immediately-prior row.
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
        -- intDiv(dateDiff('second'),60), NOT dateDiff('minute'): truncate elapsed minutes (dateDiff counts boundary crossings).
        intDiv(dateDiff('second', min(snapshot_time), max(snapshot_time)), 60) as duration_min,
        count(*) as num_fixes,
        argMin(latitude, snapshot_time)        as first_lat,
        argMin(longitude, snapshot_time)       as first_lon,
        -- tuple() keeps a NULL endpoint altitude: bare argMin SKIPS NULL args and would fall through to a
        -- later non-null fix. A NULL first_alt_m must fail `< legs_cruise_alt_m` so the endpoint stays
        -- unsnapped (origin/dest NULL).
        argMin(tuple(baro_altitude_m), snapshot_time).1 as first_alt_m,
        argMax(latitude, snapshot_time)        as last_lat,
        argMax(longitude, snapshot_time)       as last_lon,
        argMax(tuple(baro_altitude_m), snapshot_time).1 as last_alt_m
    from airborne
    group by icao24, leg_id
),
origin_snap as (
    -- Lat-bucket equi-join for the haversine snap: arrayJoin each low-alt origin into 3 floor(lat)±1
    -- buckets, equi-join dim_airports on the bucket, then the exact lat/lon/haversine residual.
    select l.icao24, l.leg_id,
           a.icao as origin_icao, a.name as origin_name, a.lat as origin_lat, a.lon as origin_lon,
           row_number() over (partition by l.icao24, l.leg_id
                              order by {{ haversine_km('l.first_lat', 'l.first_lon', 'a.lat', 'a.lon') }}) as rn
    from (
        select lg.icao24 as icao24, lg.leg_id as leg_id,
               -- explicit bare aliases: the cc join makes these joined-CTE columns (CH qualifier-leak).
               lg.first_lat as first_lat, lg.first_lon as first_lon, lg.first_alt_m as first_alt_m,
               -- Airliners don't land at unscheduled strips: gate their snap candidates (spec 2026-07-05).
               {{ airline_shaped('cc.callsign') }} as airline_shaped,
               arrayJoin([toInt32(floor(lg.first_lat)) - 1, toInt32(floor(lg.first_lat)), toInt32(floor(lg.first_lat)) + 1]) as lat_bucket
        from legs lg
        left join callsign_choice cc on cc.icao24 = lg.icao24 and cc.leg_id = lg.leg_id
        where lg.first_alt_m < {{ var('legs_cruise_alt_m') }}
    ) l
    join (select icao, name, lat, lon, scheduled_service, toInt32(floor(lat)) as lat_bucket from {{ ref('dim_airports') }}) a
      on a.lat_bucket = l.lat_bucket
    where a.lat between l.first_lat - {{ var('legs_snap_km') }} / 110.574 and l.first_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - l.first_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(l.first_lat)), 0.01))
      and {{ haversine_km('l.first_lat', 'l.first_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
      and (not l.airline_shaped or a.scheduled_service)
),
dest_snap as (
    select l.icao24, l.leg_id,
           a.icao as dest_icao, a.name as dest_name, a.lat as dest_lat, a.lon as dest_lon,
           row_number() over (partition by l.icao24, l.leg_id
                              order by {{ haversine_km('l.last_lat', 'l.last_lon', 'a.lat', 'a.lon') }}) as rn
    from (
        select lg.icao24 as icao24, lg.leg_id as leg_id,
               lg.last_lat as last_lat, lg.last_lon as last_lon, lg.last_alt_m as last_alt_m,
               {{ airline_shaped('cc.callsign') }} as airline_shaped,
               arrayJoin([toInt32(floor(lg.last_lat)) - 1, toInt32(floor(lg.last_lat)), toInt32(floor(lg.last_lat)) + 1]) as lat_bucket
        from legs lg
        left join callsign_choice cc on cc.icao24 = lg.icao24 and cc.leg_id = lg.leg_id
        where lg.last_alt_m < {{ var('legs_cruise_alt_m') }}
    ) l
    join (select icao, name, lat, lon, scheduled_service, toInt32(floor(lat)) as lat_bucket from {{ ref('dim_airports') }}) a
      on a.lat_bucket = l.lat_bucket
    where a.lat between l.last_lat - {{ var('legs_snap_km') }} / 110.574 and l.last_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - l.last_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(l.last_lat)), 0.01))
      and {{ haversine_km('l.last_lat', 'l.last_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
      and (not l.airline_shaped or a.scheduled_service)
),
antenna as (
    -- Rooftop is the only military signal; left-joined below so OpenSky context legs without it read false.
    -- bool_or -> max over the UInt8 is_military (CH has no bool_or); reads the P3a silver_ch.fct_adsb_state.
    select hex, max(is_military) as is_military
    from {{ ref('fct_adsb_state') }}
    group by hex
),
adsblol_routes as (
    -- Global-trace fallback for overflights, v6.10: legs join CHAIN windows (coverage-gap
    -- segments chained into whole flights) — window overlap, per-endpoint pick: origin from
    -- the earliest overlapping chain, dest from the latest; tuple() preserves NULL snaps.
    select l.icao24 as icao24, l.leg_id as leg_id,
           argMin(tuple(c.origin_icao), c.chain_start).1 as origin_icao,
           argMin(tuple(c.origin_name), c.chain_start).1 as origin_name,
           argMin(tuple(c.origin_lat),  c.chain_start).1 as origin_lat,
           argMin(tuple(c.origin_lon),  c.chain_start).1 as origin_lon,
           argMax(tuple(c.dest_icao), c.chain_end).1 as dest_icao,
           argMax(tuple(c.dest_name), c.chain_end).1 as dest_name,
           argMax(tuple(c.dest_lat),  c.chain_end).1 as dest_lat,
           argMax(tuple(c.dest_lon),  c.chain_end).1 as dest_lon
    from legs l
    join {{ ref('int_flight_chains_adsblol') }} c
      on c.icao24 = lower(l.icao24)
     and c.chain_start <= l.end_time
     and c.chain_end   >= l.start_time
    group by l.icao24, l.leg_id
),
curated_routes as (
    -- Curated last-resort fill (seed, evidence-backed): callsign + validity window;
    -- latest valid_from wins when windows overlap; '' in the seed = no override.
    -- An endpoint only counts as resolved once dim_airports confirms the ICAO exists.
    select icao24, leg_id, origin_icao, origin_name, origin_lat, origin_lon,
           dest_icao, dest_name, dest_lat, dest_lon
    from (
        select l.icao24 as icao24, l.leg_id as leg_id,
               oa.icao as origin_icao,
               da.icao as dest_icao,
               oa.name as origin_name, oa.lat as origin_lat, oa.lon as origin_lon,
               da.name as dest_name,   da.lat as dest_lat,   da.lon as dest_lon,
               row_number() over (partition by l.icao24, l.leg_id order by ov.valid_from desc) as rn
        from legs l
        join callsign_choice cc on cc.icao24 = l.icao24 and cc.leg_id = l.leg_id
        join {{ ref('dim_route_overrides') }} ov
          on ov.callsign = trimBoth(cc.callsign)
         and toDate(l.start_time) between ov.valid_from and ov.valid_to
        left join {{ ref('dim_airports') }} oa on oa.icao = nullIf(ov.origin_icao, '')
        left join {{ ref('dim_airports') }} da on da.icao = nullIf(ov.dest_icao, '')
    )
    where rn = 1
)
select
    *,
    case when origin_icao is not null and dest_icao is not null
         then concat(origin_icao, '-', dest_icao) end as route_inferred,
    -- Leg-level summary (v6.9 shape extended): trace lane wins the label when it contributed,
    -- then curated, then pure-snap; per-endpoint truth lives in origin_source/dest_source.
    multiIf(origin_source = 'adsblol' or dest_source = 'adsblol', 'adsblol',
            origin_source = 'curated' or dest_source = 'curated', 'curated',
            origin_source = 'snap'    or dest_source = 'snap',    'snap',
            null) as route_source
from (
    select
        -- CH keeps the table qualifier in the output column name for `alias.col` (icao24/leg_id/callsign
        -- appear across the joined CTEs), so alias them explicitly to bare names (downstream + parity).
        l.icao24 as icao24,
        l.leg_id as leg_id,
        cc.callsign as callsign,
        l.start_time,
        l.end_time,
        l.duration_min,
        l.num_fixes,
        l.first_lat, l.first_lon, l.first_alt_m,
        l.last_lat,  l.last_lon,  l.last_alt_m,
        multiIf(o.origin_icao is not null, o.origin_icao,
                ar.origin_icao is not null, ar.origin_icao,
                cr.origin_icao) as origin_icao,
        multiIf(o.origin_icao is not null, o.origin_name,
                ar.origin_icao is not null, ar.origin_name,
                cr.origin_name) as origin_name,
        multiIf(o.origin_icao is not null, o.origin_lat,
                ar.origin_icao is not null, ar.origin_lat,
                cr.origin_lat) as origin_lat,
        multiIf(o.origin_icao is not null, o.origin_lon,
                ar.origin_icao is not null, ar.origin_lon,
                cr.origin_lon) as origin_lon,
        multiIf(d.dest_icao is not null, d.dest_icao,
                ar.dest_icao is not null, ar.dest_icao,
                cr.dest_icao) as dest_icao,
        multiIf(d.dest_icao is not null, d.dest_name,
                ar.dest_icao is not null, ar.dest_name,
                cr.dest_name) as dest_name,
        multiIf(d.dest_icao is not null, d.dest_lat,
                ar.dest_icao is not null, ar.dest_lat,
                cr.dest_lat) as dest_lat,
        multiIf(d.dest_icao is not null, d.dest_lon,
                ar.dest_icao is not null, ar.dest_lon,
                cr.dest_lon) as dest_lon,
        multiIf(o.origin_icao is not null, 'snap',
                ar.origin_icao is not null, 'adsblol',
                cr.origin_icao is not null, 'curated',
                null) as origin_source,
        multiIf(d.dest_icao is not null, 'snap',
                ar.dest_icao is not null, 'adsblol',
                cr.dest_icao is not null, 'curated',
                null) as dest_source,
        ac.registration,
        ac.typecode,
        al.name    as airline_name,
        al.country as airline_country,
        -- reg_country via the P1 range_hashed dict; macro guards '~' hexes.
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
    left join adsblol_routes ar on ar.icao24 = l.icao24 and ar.leg_id = l.leg_id
    left join curated_routes cr on cr.icao24 = l.icao24 and cr.leg_id = l.leg_id
)
