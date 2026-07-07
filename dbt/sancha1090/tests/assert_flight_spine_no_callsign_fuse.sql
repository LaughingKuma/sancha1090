-- D4.1 over-merge guard: every non-null-callsign opensky_flights opinion must be represented by an
-- opensky_flights anchor of the SAME callsign overlapping it. A NULL-bridge fusion swallows a callsign
-- into a different-callsign anchor, leaving it with no anchor of its own -> this fires. (Window containment
-- can't be used: a different-callsign cluster may legitimately start inside a longer anchor's window.)
with op as (
    select icao24, win_start, win_end, trimBoth(callsign) as cs
    from {{ ref('int_flight_opinions') }} where source = 'opensky_flights' and callsign is not null
),
anch as (
    select icao24, flight_start, flight_end, trimBoth(anchor_callsign) as cs
    from {{ ref('int_flight_spine') }} where anchor_source = 'opensky_flights' and anchor_callsign is not null
)
select o.icao24, o.win_start, o.cs
from op o
left join anch a on a.icao24 = o.icao24
group by o.icao24, o.win_start, o.win_end, o.cs
-- countIf (not sum(if)): a no-match left join yields NULL, and sum(NULL)=NULL would slip past `=0`.
having countIf(a.cs = o.cs and a.flight_start <= o.win_end and a.flight_end >= o.win_start) = 0
