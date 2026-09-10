-- #213A regression: recompute the snap tier with its own airline/distance/feasibility expressions (not
-- the shared macros) and confirm it matches -- bounded to the trailing 3 days so a regression still shows same-run.
with segs as (
    select icao24, seg_start_time, callsign,
           first_lat, first_lon, first_on_ground, first_alt_m,
           last_lat, last_lon, last_on_ground, last_alt_m,
           (callsign is not null and match(trimBoth(callsign), '^[A-Z]{3}[0-9]')) as airline_shaped,
           (j.icao24 is not null) as is_jet
    from {{ ref('stg_flight_segments_adsblol') }} s
    left join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(s.icao24)
    where s.trace_day >= today() - 2
),
airports as (
    select icao, lat, lon, scheduled_service, airport_type, runway_length_ft,
           toInt32(floor(lat)) as lat_bucket
    from {{ ref('dim_airports') }}
),
origin_cand as (
    select s.icao24, s.seg_start_time, a.icao as cand_icao, a.airport_type as cand_type,
           (2 * 6371 * asin(sqrt(
               power(sin(radians(a.lat - s.first_lat) / 2), 2)
               + cos(radians(s.first_lat)) * cos(radians(a.lat))
                 * power(sin(radians(a.lon - s.first_lon) / 2), 2)
           ))) as dist
    from (
        select *, arrayJoin([toInt32(floor(first_lat)) - 1, toInt32(floor(first_lat)), toInt32(floor(first_lat)) + 1]) as lat_bucket
        from segs
        where first_on_ground or first_alt_m < {{ var('legs_cruise_alt_m') }}
    ) s
    join airports a on a.lat_bucket = s.lat_bucket
    where a.lat between s.first_lat - {{ var('legs_snap_km') }} / 110.574 and s.first_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - s.first_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(s.first_lat)), 0.01))
      and (not s.airline_shaped or a.scheduled_service)
      and not (s.airline_shaped and s.is_jet
               and ((coalesce(a.runway_length_ft, 0) > 0 and coalesce(a.runway_length_ft, 0) < {{ var('jet_min_runway_ft') }})
                    or (coalesce(a.airport_type, '') = 'small_airport' and coalesce(a.runway_length_ft, 0) = 0)))
),
origin_recomputed as (
    select icao24, seg_start_time, cand_icao,
           row_number() over (partition by icao24, seg_start_time
               order by if(cand_type not in ('heliport', 'seaplane_base') and dist <= {{ var('snap_iata_pref_km') }}, 0, 1),
                        dist, cand_icao) as rn
    from origin_cand
    where dist <= {{ var('legs_snap_km') }}
),
dest_cand as (
    select s.icao24, s.seg_start_time, a.icao as cand_icao, a.airport_type as cand_type,
           (2 * 6371 * asin(sqrt(
               power(sin(radians(a.lat - s.last_lat) / 2), 2)
               + cos(radians(s.last_lat)) * cos(radians(a.lat))
                 * power(sin(radians(a.lon - s.last_lon) / 2), 2)
           ))) as dist
    from (
        select *, arrayJoin([toInt32(floor(last_lat)) - 1, toInt32(floor(last_lat)), toInt32(floor(last_lat)) + 1]) as lat_bucket
        from segs
        where last_on_ground or last_alt_m < {{ var('legs_cruise_alt_m') }}
    ) s
    join airports a on a.lat_bucket = s.lat_bucket
    where a.lat between s.last_lat - {{ var('legs_snap_km') }} / 110.574 and s.last_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - s.last_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(s.last_lat)), 0.01))
      and (not s.airline_shaped or a.scheduled_service)
      and not (s.airline_shaped and s.is_jet
               and ((coalesce(a.runway_length_ft, 0) > 0 and coalesce(a.runway_length_ft, 0) < {{ var('jet_min_runway_ft') }})
                    or (coalesce(a.airport_type, '') = 'small_airport' and coalesce(a.runway_length_ft, 0) = 0)))
),
dest_recomputed as (
    select icao24, seg_start_time, cand_icao,
           row_number() over (partition by icao24, seg_start_time
               order by if(cand_type not in ('heliport', 'seaplane_base') and dist <= {{ var('snap_iata_pref_km') }}, 0, 1),
                        dist, cand_icao) as rn
    from dest_cand
    where dist <= {{ var('legs_snap_km') }}
),
-- bounded before the join, not just in the WHERE, else the LEFT JOIN below (needed for NULL-safety)
-- would compare every historical row against an empty recomputed side and flood false positives.
recent_routes as (
    select icao24, seg_start_time, origin_icao, dest_icao
    from {{ ref('int_flight_routes_adsblol') }}
    where trace_day >= today() - 2
)
select r.icao24, r.seg_start_time, 'origin' as endpoint, r.origin_icao as actual, o.cand_icao as recomputed
from recent_routes r
left join origin_recomputed o on o.icao24 = r.icao24 and o.seg_start_time = r.seg_start_time and o.rn = 1
where coalesce(r.origin_icao, '') != coalesce(o.cand_icao, '')
union all
select r.icao24, r.seg_start_time, 'dest' as endpoint, r.dest_icao as actual, d.cand_icao as recomputed
from recent_routes r
left join dest_recomputed d on d.icao24 = r.icao24 and d.seg_start_time = r.seg_start_time and d.rn = 1
where coalesce(r.dest_icao, '') != coalesce(d.cand_icao, '')
