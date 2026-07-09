{{ config(materialized='table', tags=['reconcile']) }}

-- One windowed O/D opinion per source, common schema, authority-ranked (1 = highest; plurality still
-- outvotes authority -- rank only breaks ties). Curated is NOT here -- it's windowless and applied as
-- an override in fct_flights_reconciled. vrs_routes (rank 2) is not here either: standing data
-- carries no window, so it votes at int_flight_attach and never anchors.
with raw_opinions as (
    select 'swim' as source, toUInt8(1) as source_rank,
           icao24, win_start, win_end, callsign, origin_icao, dest_icao
    from {{ ref('int_swim_opinion') }}
    where icao24 is not null and win_start is not null and win_end is not null
    union all
    select 'opensky_flights' as source, toUInt8(3) as source_rank,
           icao24, first_seen as win_start, last_seen as win_end, callsign, origin_icao, dest_icao
    from {{ ref('fact_flights') }}
    where icao24 is not null and first_seen is not null and last_seen is not null
      and (toUnixTimestamp(last_seen) - toUnixTimestamp(first_seen)) / 3600.0 <= {{ var('reconcile_anchor_max_hours') }}
      -- OpenSky fuses rotations under a sticky callsign; a resolved O/D far too near for the window
      -- length is the same defect in-band. Filtered here so a fused window neither anchors nor votes.
      and not (origin_lat is not null and origin_lon is not null
               and dest_lat is not null and dest_lon is not null
               and (toUnixTimestamp(last_seen) - toUnixTimestamp(first_seen)) / 3600.0 >= 8
               and {{ haversine_km('origin_lat', 'origin_lon', 'dest_lat', 'dest_lon') }}
                     < {{ var('fused_envelope_speed_kmh') }}
                       * ((toUnixTimestamp(last_seen) - toUnixTimestamp(first_seen)) / 3600.0 - 1.5))
    union all
    select 'adsblol' as source, toUInt8(4) as source_rank,
           icao24, chain_start as win_start, chain_end as win_end, callsign, origin_icao, dest_icao
    from {{ ref('int_flight_chains_adsblol') }}
    where icao24 is not null and chain_start is not null and chain_end is not null
      -- backstop for fused chains the boundary arms can't split (550-650 km/h headwind-ambiguous gaps)
      and (toUnixTimestamp(chain_end) - toUnixTimestamp(chain_start)) / 3600.0 <= {{ var('reconcile_anchor_max_hours') }}
    union all
    select 'opensky_states' as source, toUInt8(5) as source_rank,
           icao24, start_time as win_start, end_time as win_end, callsign, origin_icao, dest_icao
    from {{ ref('int_flight_legs_opensky') }}
    where icao24 is not null and start_time is not null and end_time is not null
)
-- SP4 runway feasibility gate, uniform across sources (the only reach into opensky_flights, whose O/D
-- is OpenSky's own estimate): airline-shaped jets only -- bizjets legitimately use short strips. NULL the
-- endpoint so it can't vote; the window still anchors the flight.
select
    o.source, o.source_rank, o.icao24 as icao24, o.win_start, o.win_end, o.callsign,
    if({{ jet_infeasible_endpoint(airline_shaped('o.callsign'), 'j.icao24 is not null', 'oa.runway_length_ft', 'oa.airport_type') }},
       NULL, o.origin_icao) as origin_icao,
    if({{ jet_infeasible_endpoint(airline_shaped('o.callsign'), 'j.icao24 is not null', 'da.runway_length_ft', 'da.airport_type') }},
       NULL, o.dest_icao) as dest_icao
from raw_opinions o
left join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(o.icao24)
left join {{ ref('dim_airports') }} oa on oa.icao = o.origin_icao
left join {{ ref('dim_airports') }} da on da.icao = o.dest_icao
