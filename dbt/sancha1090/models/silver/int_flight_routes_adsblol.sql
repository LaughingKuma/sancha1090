{{ config(materialized='table', tags=['adsblol']) }}

-- Airport-snap of global segment endpoints: the same lat-bucket equi-join, haversine
-- and thresholds as fct_flight_legs, but over the WORLD — segments end at airports the
-- Japan box never sees. An endpoint is snappable on the ground or below cruise.
with segs as (
    select * from {{ ref('stg_flight_segments_adsblol') }}
),
origin_snap as (
    select s.icao24, s.seg_start_time,
           a.icao as origin_icao, a.name as origin_name, a.lat as origin_lat, a.lon as origin_lon,
           -- IATA-bearing airports within snap_iata_pref_km outrank a marginally-nearer no-IATA
           -- field (seaplane bases/heliports); beyond that pure nearest, unchanged.
           row_number() over (partition by s.icao24, s.seg_start_time
                              order by if(a.iata != '' and {{ haversine_km('s.first_lat', 's.first_lon', 'a.lat', 'a.lon') }} <= {{ var('snap_iata_pref_km') }}, 0, 1),
                                       {{ haversine_km('s.first_lat', 's.first_lon', 'a.lat', 'a.lon') }}) as rn
    from (
        select icao24, seg_start_time, first_lat, first_lon,
               arrayJoin([toInt32(floor(first_lat)) - 1, toInt32(floor(first_lat)), toInt32(floor(first_lat)) + 1]) as lat_bucket
        from segs
        where first_on_ground or first_alt_m < {{ var('legs_cruise_alt_m') }}
    ) s
    join (select icao, iata, name, lat, lon, toInt32(floor(lat)) as lat_bucket from {{ ref('dim_airports') }}) a
      on a.lat_bucket = s.lat_bucket
    where a.lat between s.first_lat - {{ var('legs_snap_km') }} / 110.574 and s.first_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - s.first_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(s.first_lat)), 0.01))
      and {{ haversine_km('s.first_lat', 's.first_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
),
dest_snap as (
    select s.icao24, s.seg_start_time,
           a.icao as dest_icao, a.name as dest_name, a.lat as dest_lat, a.lon as dest_lon,
           -- IATA-bearing airports within snap_iata_pref_km outrank a marginally-nearer no-IATA
           -- field (seaplane bases/heliports); beyond that pure nearest, unchanged.
           row_number() over (partition by s.icao24, s.seg_start_time
                              order by if(a.iata != '' and {{ haversine_km('s.last_lat', 's.last_lon', 'a.lat', 'a.lon') }} <= {{ var('snap_iata_pref_km') }}, 0, 1),
                                       {{ haversine_km('s.last_lat', 's.last_lon', 'a.lat', 'a.lon') }}) as rn
    from (
        select icao24, seg_start_time, last_lat, last_lon,
               arrayJoin([toInt32(floor(last_lat)) - 1, toInt32(floor(last_lat)), toInt32(floor(last_lat)) + 1]) as lat_bucket
        from segs
        where last_on_ground or last_alt_m < {{ var('legs_cruise_alt_m') }}
    ) s
    join (select icao, iata, name, lat, lon, toInt32(floor(lat)) as lat_bucket from {{ ref('dim_airports') }}) a
      on a.lat_bucket = s.lat_bucket
    where a.lat between s.last_lat - {{ var('legs_snap_km') }} / 110.574 and s.last_lat + {{ var('legs_snap_km') }} / 110.574
      and abs(modulo(a.lon - s.last_lon + 540, 360) - 180)
            <= {{ var('legs_snap_km') }} / (111.32 * greatest(cos(radians(s.last_lat)), 0.01))
      and {{ haversine_km('s.last_lat', 's.last_lon', 'a.lat', 'a.lon') }} <= {{ var('legs_snap_km') }}
)
select
    s.icao24 as icao24,
    s.callsign as callsign,
    -- explicit bare alias: seg_start_time is ambiguous across o/d, so CH would otherwise keep the `s.` qualifier in the column name.
    s.seg_start_time as seg_start_time,
    s.seg_end_time,
    s.num_fixes,
    s.trace_day,
    o.origin_icao, o.origin_name, o.origin_lat, o.origin_lon,
    d.dest_icao,   d.dest_name,   d.dest_lat,   d.dest_lon
from segs s
left join (select * from origin_snap where rn = 1) o
       on o.icao24 = s.icao24 and o.seg_start_time = s.seg_start_time
left join (select * from dest_snap where rn = 1) d
       on d.icao24 = s.icao24 and d.seg_start_time = s.seg_start_time
