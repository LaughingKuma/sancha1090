{{ config(materialized='table', tags=['reconcile']) }}

-- OpenSky-states O/D opinion for the reconciler: sessionize + pure sched-gated snap (no adsblol/curated
-- fallback) + callsign-flip split. Duplicates fct_flight_legs's sessionize+snap on purpose (SP2 dedups).
with ordered as (
    select
        icao24, snapshot_time, latitude, longitude, baro_altitude_m, on_ground, callsign,
        lagInFrame(toNullable(snapshot_time), 1, NULL)
            over (partition by icao24 order by snapshot_time rows between 1 preceding and current row) as prev_time,
        lagInFrame(toNullable(on_ground), 1, NULL)
            over (partition by icao24 order by snapshot_time rows between 1 preceding and current row) as prev_on_ground,
        lagInFrame(toNullable(callsign), 1, NULL)
            over (partition by icao24 order by snapshot_time rows between 1 preceding and current row) as prev_callsign
    from {{ ref('fact_state_snapshots') }}
),
flagged as (
    select *,
        case
            when prev_time is null then 1
            when dateDiff('second', prev_time, snapshot_time) > {{ var('legs_gap_min') }} * 60 then 1
            when on_ground and not prev_on_ground then 1
            when prev_callsign is not null and callsign is not null and callsign != prev_callsign
                 and dateDiff('second', prev_time, snapshot_time) > {{ var('legs_turnaround_min') }} * 60 then 1
            else 0
        end as leg_break
    from ordered
),
legged as (
    select *,
        sum(leg_break) over (partition by icao24 order by snapshot_time rows between unbounded preceding and current row) as leg_id
    from flagged
),
airborne as (select * from legged where not on_ground),
callsign_choice as (
    select icao24, leg_id, callsign
    from (
        select icao24, leg_id, callsign,
               row_number() over (partition by icao24, leg_id order by cs_cnt desc, first_seen asc, callsign asc) as rn
        from (
            select icao24, leg_id, callsign, count(*) as cs_cnt, min(snapshot_time) as first_seen
            from airborne where callsign is not null group by icao24, leg_id, callsign
        )
    ) where rn = 1
),
legs as (
    select icao24, leg_id,
        min(snapshot_time) as start_time, max(snapshot_time) as end_time, count(*) as num_fixes,
        argMin(latitude, snapshot_time) as first_lat,
        argMin(longitude, snapshot_time) as first_lon,
        argMin(tuple(baro_altitude_m), snapshot_time).1 as first_alt_m,
        argMax(latitude, snapshot_time) as last_lat,
        argMax(longitude, snapshot_time) as last_lon,
        argMax(tuple(baro_altitude_m), snapshot_time).1 as last_alt_m
    from airborne group by icao24, leg_id
),
origin_snap as (
    select l.icao24, l.leg_id,
           a.icao as origin_icao, a.name as origin_name, a.lat as origin_lat, a.lon as origin_lon,
           row_number() over (partition by l.icao24, l.leg_id
                              order by {{ haversine_km('l.first_lat', 'l.first_lon', 'a.lat', 'a.lon') }}) as rn
    from (
        select lg.icao24 as icao24, lg.leg_id as leg_id,
               lg.first_lat as first_lat, lg.first_lon as first_lon, lg.first_alt_m as first_alt_m,
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
)
select
    l.icao24 as icao24,
    l.leg_id as leg_id,
    cc.callsign as callsign,
    l.start_time, l.end_time, l.num_fixes,
    l.first_lat, l.first_lon, l.first_alt_m,
    l.last_lat, l.last_lon, l.last_alt_m,
    o.origin_icao, o.origin_name, o.origin_lat, o.origin_lon,
    d.dest_icao, d.dest_name, d.dest_lat, d.dest_lon
from legs l
left join callsign_choice cc on cc.icao24 = l.icao24 and cc.leg_id = l.leg_id
left join (select * from origin_snap where rn = 1) o on o.icao24 = l.icao24 and o.leg_id = l.leg_id
left join (select * from dest_snap   where rn = 1) d on d.icao24 = l.icao24 and d.leg_id = l.leg_id
