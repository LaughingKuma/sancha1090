-- #150: D1's 16h cap catches only the extreme tail of this class (1 of 61 at fix time); this pins the
-- exact break property -- dest(leg_n) == origin(leg_n+1), incl. same-airport rotations and NULL-endpoint legs.
with o as (
    select distinct icao24, win_start, win_end, origin_icao, dest_icao
    from {{ ref('int_flight_opinions') }}
    where source = 'opensky_flights'
),
s as (
    select flight_id, icao24, flight_start, flight_end
    from {{ ref('int_flight_spine') }} where anchor_source = 'opensky_flights'
)
select s.flight_id, s.icao24,
       o1.win_start as o1_win_start, o1.win_end as o1_win_end, o1.origin_icao as o1_origin, o1.dest_icao as o1_dest,
       o2.win_start as o2_win_start, o2.win_end as o2_win_end, o2.origin_icao as o2_origin, o2.dest_icao as o2_dest
from s
inner join o o1 on o1.icao24 = s.icao24 and o1.win_start >= s.flight_start and o1.win_end <= s.flight_end
inner join o o2 on o2.icao24 = s.icao24 and o2.win_start >= s.flight_start and o2.win_end <= s.flight_end
where o2.win_start >= o1.win_end
  -- a zero-length opinion satisfies win_start >= win_end against ITSELF; a pair must be two distinct rows
  and (o1.win_start, o1.win_end) != (o2.win_start, o2.win_end)
  and o1.dest_icao = o2.origin_icao
