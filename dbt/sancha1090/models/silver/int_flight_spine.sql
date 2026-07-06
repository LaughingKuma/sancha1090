{{ config(materialized='table', tags=['reconcile']) }}

-- Flight spine: one row per authority-ranked flight anchor. Authority-ranked anchoring -- opensky_flights first,
-- adsb.lol only where it overlaps no callsign-compatible flight-summary, opensky_states last.
-- A collapsed low-authority record can never anchor when a cleaner source exists, so it can never
-- merge two flights. CH ON is equi-only -> overlap anti-join via equi(icao24)+HAVING count=0.
with op_flights as (
    select icao24, win_start, win_end, callsign from {{ ref('int_flight_opinions') }} where source = 'opensky_flights'
),
adsblol as (
    select icao24, win_start, win_end, callsign from {{ ref('int_flight_opinions') }} where source = 'adsblol'
),
opensky_states as (
    select icao24, win_start, win_end, callsign from {{ ref('int_flight_opinions') }} where source = 'opensky_states'
),
adsblol_anchors as (
    select a.icao24 as icao24, a.win_start as win_start, a.win_end as win_end, a.callsign as callsign
    from adsblol a
    left join op_flights f on f.icao24 = a.icao24
    group by a.icao24, a.win_start, a.win_end, a.callsign
    having sum(if(f.icao24 is not null
                  and f.win_start <= a.win_end and f.win_end >= a.win_start
                  and (f.callsign = a.callsign or f.callsign is null or a.callsign is null), 1, 0)) = 0
),
higher as (
    select icao24, win_start, win_end, callsign from op_flights
    union all
    select icao24, win_start, win_end, callsign from adsblol_anchors
),
states_anchors as (
    select a.icao24 as icao24, a.win_start as win_start, a.win_end as win_end, a.callsign as callsign
    from opensky_states a
    left join higher h on h.icao24 = a.icao24
    group by a.icao24, a.win_start, a.win_end, a.callsign
    having sum(if(h.icao24 is not null
                  and h.win_start <= a.win_end and h.win_end >= a.win_start
                  and (h.callsign = a.callsign or h.callsign is null or a.callsign is null), 1, 0)) = 0
),
all_anchors as (
    select icao24, win_start, win_end, callsign, 'opensky_flights' as anchor_source, toUInt8(1) as anchor_rank from op_flights
    union all
    select icao24, win_start, win_end, callsign, 'adsblol' as anchor_source, toUInt8(2) as anchor_rank from adsblol_anchors
    union all
    select icao24, win_start, win_end, callsign, 'opensky_states' as anchor_source, toUInt8(3) as anchor_rank from states_anchors
)
select
    cityHash64(icao24, toString(win_start), anchor_source) as flight_id,
    icao24,
    win_start as flight_start,
    win_end   as flight_end,
    callsign  as anchor_callsign,
    anchor_source,
    anchor_rank
from all_anchors
