-- D4.1: after clustering, no two opensky_flights anchors for one icao24 sit within anchor_merge_gap_min
-- with a compatible callsign. Compatible = equal or either blank (mirrors the merge rule).
with a as (
    select icao24, flight_start, flight_end, anchor_callsign
    from {{ ref('int_flight_spine') }} where anchor_source = 'opensky_flights'
)
select a1.icao24
from a a1
join a a2 on a2.icao24 = a1.icao24
where a1.flight_start < a2.flight_start
  and a2.flight_start <= a1.flight_end + interval {{ var('anchor_merge_gap_min') }} minute
  and (a1.anchor_callsign = a2.anchor_callsign or a1.anchor_callsign is null or a2.anchor_callsign is null)
