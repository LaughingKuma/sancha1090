{{ config(materialized='table', tags=['reconcile']) }}

-- OpenSky-states O/D opinion for the reconciler: sessionize + pure sched-gated snap + callsign-flip split.
-- fct_flight_legs consumes this model (SP2 dedup); the canonical OpenSky-states sessionize+snap opinion.
-- A run of identical fixes never crosses a legs_gap_min session boundary: past the gap it is the next leg's
-- first fix, not a stale tail. NULL-safe compares so a NULL never folds into the previous run and gets dropped.
with streaked as (
    select *,
        sum(if(prev_time is null or dateDiff('second', prev_time, snapshot_time) > {{ var('legs_gap_min') }} * 60
               or not (latitude <=> prev_lat) or not (longitude <=> prev_lon) or not (on_ground <=> prev_ground), 1, 0))
            over (partition by icao24 order by snapshot_time rows between unbounded preceding and current row) as streak_id
    from (
        select icao24, snapshot_time, latitude, longitude, baro_altitude_m, on_ground, callsign,
            {{ prev_fix('snapshot_time') }} as prev_time,
            {{ prev_fix('latitude') }} as prev_lat,
            {{ prev_fix('longitude') }} as prev_lon,
            {{ prev_fix('on_ground') }} as prev_ground
        from {{ ref('fact_state_snapshots') }}
    ) lagged
),
-- OpenSky re-serves a lost aircraft's last position, so an airborne run of identical fixes longer than a
-- turnaround is coverage loss, not tracking: the stale tail must neither bridge a gap nor count as fixes.
thawed as (
    select icao24, snapshot_time, latitude, longitude, baro_altitude_m, on_ground, callsign
    from (
        select *,
            min(snapshot_time) over (partition by icao24, streak_id) as streak_start,
            max(snapshot_time) over (partition by icao24, streak_id) as streak_end
        from streaked
    ) bounded
    where on_ground or snapshot_time = streak_start
       or dateDiff('second', streak_start, streak_end) <= {{ var('legs_frozen_min') }} * 60
),
ordered as (
    select
        icao24, snapshot_time, latitude, longitude, baro_altitude_m, on_ground, callsign,
        {{ prev_fix('snapshot_time') }} as prev_time,
        {{ prev_fix('on_ground') }} as prev_on_ground,
        {{ prev_fix('callsign') }} as prev_callsign
    from thawed
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
-- one fix is one position: it would snap the same field at both ends and vote a same-airport flight.
-- Suppress the vote, keep the leg -- its anchor is what lets VRS/swim resolve the flight by callsign.
votable_legs as (select * from legs where num_fixes >= 2),
origin_snap as (
    select l.icao24, l.leg_id,
           a.icao as origin_icao, a.name as origin_name, a.lat as origin_lat, a.lon as origin_lon,
           row_number() over (partition by l.icao24, l.leg_id
                              order by {{ snap_order('l.first_lat', 'l.first_lon') }}) as rn
    from (
        select lg.icao24 as icao24, lg.leg_id as leg_id,
               lg.first_lat as first_lat, lg.first_lon as first_lon, lg.first_alt_m as first_alt_m,
               {{ airline_shaped('cc.callsign') }} as airline_shaped,
               (j.icao24 is not null) as is_jet,
               arrayJoin([toInt32(floor(lg.first_lat)) - 1, toInt32(floor(lg.first_lat)), toInt32(floor(lg.first_lat)) + 1]) as lat_bucket
        from votable_legs lg
        left join callsign_choice cc on cc.icao24 = lg.icao24 and cc.leg_id = lg.leg_id
        left join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(lg.icao24)
        where lg.first_alt_m < {{ var('legs_cruise_alt_m') }}
    ) l
    join (select icao, name, lat, lon, scheduled_service, airport_type, runway_length_ft,
                 toInt32(floor(lat)) as lat_bucket from {{ ref('dim_airports') }}) a
      on a.lat_bucket = l.lat_bucket
    where a.lat between l.first_lat - {{ var('legs_snap_km') }} / 110.574 and l.first_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - l.first_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(l.first_lat)), 0.01))
      and {{ haversine_km('l.first_lat', 'l.first_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
      and (not l.airline_shaped or a.scheduled_service)
      -- SP4: an airline-shaped jet can't use this field -> next-nearest feasible candidate wins (repair, not NULL)
      and not {{ jet_infeasible_endpoint('l.airline_shaped', 'l.is_jet', 'a.runway_length_ft', 'a.airport_type') }}
),
dest_snap as (
    select l.icao24, l.leg_id,
           a.icao as dest_icao, a.name as dest_name, a.lat as dest_lat, a.lon as dest_lon,
           row_number() over (partition by l.icao24, l.leg_id
                              order by {{ snap_order('l.last_lat', 'l.last_lon') }}) as rn
    from (
        select lg.icao24 as icao24, lg.leg_id as leg_id,
               lg.last_lat as last_lat, lg.last_lon as last_lon, lg.last_alt_m as last_alt_m,
               {{ airline_shaped('cc.callsign') }} as airline_shaped,
               (j.icao24 is not null) as is_jet,
               arrayJoin([toInt32(floor(lg.last_lat)) - 1, toInt32(floor(lg.last_lat)), toInt32(floor(lg.last_lat)) + 1]) as lat_bucket
        from votable_legs lg
        left join callsign_choice cc on cc.icao24 = lg.icao24 and cc.leg_id = lg.leg_id
        left join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(lg.icao24)
        where lg.last_alt_m < {{ var('legs_cruise_alt_m') }}
    ) l
    join (select icao, name, lat, lon, scheduled_service, airport_type, runway_length_ft,
                 toInt32(floor(lat)) as lat_bucket from {{ ref('dim_airports') }}) a
      on a.lat_bucket = l.lat_bucket
    where a.lat between l.last_lat - {{ var('legs_snap_km') }} / 110.574 and l.last_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - l.last_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(l.last_lat)), 0.01))
      and {{ haversine_km('l.last_lat', 'l.last_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
      and (not l.airline_shaped or a.scheduled_service)
      -- SP4: an airline-shaped jet can't use this field -> next-nearest feasible candidate wins (repair, not NULL)
      and not {{ jet_infeasible_endpoint('l.airline_shaped', 'l.is_jet', 'a.runway_length_ft', 'a.airport_type') }}
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
