{{ config(materialized='table', tags=['adsblol']) }}

-- Chains coverage-gap-split segments into whole flights: a boundary chains when both sides are
-- airborne and the implied great-circle groundspeed across the gap is cruise-plausible. A ground
-- stop reads implausibly slow (GTI517->GTI518 sat 6.9h at the same spot); a trace-day split's tiny
-- gap degenerates to true groundspeed and chains -- so chains cross trace_day (D-1 defect closed).
with segs as (
    select
        s.icao24 as icao24,
        s.callsign as callsign,
        s.seg_start_time as seg_start_time,
        s.seg_end_time as seg_end_time,
        s.num_fixes,
        s.first_lat, s.first_lon, s.first_on_ground,
        s.last_lat, s.last_lon, s.last_on_ground,
        r.origin_icao, r.origin_name, r.origin_lat, r.origin_lon,
        r.dest_icao, r.dest_name, r.dest_lat, r.dest_lon
    from {{ ref('stg_flight_segments_adsblol') }} s
    left join {{ ref('int_flight_routes_adsblol') }} r
           on r.icao24 = s.icao24 and r.seg_start_time = s.seg_start_time
    where s.icao24 is not null and s.seg_start_time is not null
),
boundaries as (
    select *,
        lagInFrame(toNullable(seg_end_time), 1, NULL)
            over (partition by icao24 order by seg_start_time, seg_end_time rows between 1 preceding and current row) as prev_end_time,
        lagInFrame(toNullable(last_lat), 1, NULL)
            over (partition by icao24 order by seg_start_time, seg_end_time rows between 1 preceding and current row) as prev_last_lat,
        lagInFrame(toNullable(last_lon), 1, NULL)
            over (partition by icao24 order by seg_start_time, seg_end_time rows between 1 preceding and current row) as prev_last_lon,
        lagInFrame(toNullable(last_on_ground), 1, NULL)
            over (partition by icao24 order by seg_start_time, seg_end_time rows between 1 preceding and current row) as prev_last_on_ground
    from segs
),
flagged as (
    select *,
        case
            when prev_end_time is null then 1
            when dateDiff('second', prev_end_time, seg_start_time) <= 0 then 1
            when coalesce(prev_last_on_ground, true) or first_on_ground then 1
            when {{ haversine_km('prev_last_lat', 'prev_last_lon', 'first_lat', 'first_lon') }}
                     / (dateDiff('second', prev_end_time, seg_start_time) / 3600.0)
                   not between {{ var('chain_speed_min_kmh') }} and {{ var('chain_speed_max_kmh') }} then 1
            else 0
        end as chain_break
    from boundaries
),
chained as (
    select *,
        sum(chain_break) over (
            partition by icao24 order by seg_start_time, seg_end_time
            rows between unbounded preceding and current row
        ) as chain_seq
    from flagged
),
callsign_pick as (
    -- Dominant callsign per chain (by fixes, ties earliest-seen then lexical) for livemap keying.
    select icao24, chain_seq, callsign
    from (
        select icao24, chain_seq, callsign,
               row_number() over (partition by icao24, chain_seq
                                  order by cs_fixes desc, first_seen asc, callsign asc) as rn
        from (
            select icao24, chain_seq, callsign,
                   sum(num_fixes)      as cs_fixes,
                   min(seg_start_time) as first_seen
            from chained
            where callsign is not null
            group by icao24, chain_seq, callsign
        )
    )
    where rn = 1
),
chains as (
    select
        icao24,
        chain_seq,
        min(seg_start_time) as chain_start,
        max(seg_end_time)   as chain_end,
        count(*)            as num_segments,
        sum(num_fixes)      as num_fixes,
        -- tuple() keeps a NULL snap (bare argMin/argMax SKIP NULLs): the chain origin IS the first
        -- segment's snap even when NULL (cruise entry -> unresolved), never a later segment's.
        argMin(tuple(origin_icao), seg_start_time).1 as origin_icao,
        argMin(tuple(origin_name), seg_start_time).1 as origin_name,
        argMin(tuple(origin_lat),  seg_start_time).1 as origin_lat,
        argMin(tuple(origin_lon),  seg_start_time).1 as origin_lon,
        argMax(tuple(dest_icao), seg_end_time).1 as dest_icao,
        argMax(tuple(dest_name), seg_end_time).1 as dest_name,
        argMax(tuple(dest_lat),  seg_end_time).1 as dest_lat,
        argMax(tuple(dest_lon),  seg_end_time).1 as dest_lon,
        uniqExactIf(callsign, callsign is not null) <= 1 as callsign_consistent
    from chained
    group by icao24, chain_seq
)
select
    c.icao24 as icao24,
    cp.callsign as callsign,
    c.chain_start as chain_start,
    c.chain_end as chain_end,
    c.num_segments,
    c.num_fixes,
    c.callsign_consistent,
    c.origin_icao, c.origin_name, c.origin_lat, c.origin_lon,
    c.dest_icao,   c.dest_name,   c.dest_lat,   c.dest_lon
from chains c
left join callsign_pick cp on cp.icao24 = c.icao24 and cp.chain_seq = c.chain_seq
