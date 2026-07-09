{{ config(materialized='table', tags=['reconcile']) }}

-- One windowed O/D opinion per source, common schema, authority-ranked (1 = highest; plurality still
-- outvotes authority -- rank only breaks ties). Curated is NOT here -- it's windowless and applied as
-- an override in fct_flights_reconciled.
select 'swim' as source, toUInt8(1) as source_rank,
       icao24, win_start, win_end, callsign, origin_icao, dest_icao
from {{ ref('int_swim_opinion') }}
where icao24 is not null and win_start is not null and win_end is not null
union all
select 'opensky_flights' as source, toUInt8(2) as source_rank,
       icao24, first_seen as win_start, last_seen as win_end, callsign, origin_icao, dest_icao
from {{ ref('fact_flights') }}
where icao24 is not null and first_seen is not null and last_seen is not null
union all
select 'adsblol' as source, toUInt8(3) as source_rank,
       icao24, chain_start as win_start, chain_end as win_end, callsign, origin_icao, dest_icao
from {{ ref('int_flight_chains_adsblol') }}
where icao24 is not null and chain_start is not null and chain_end is not null
union all
select 'opensky_states' as source, toUInt8(4) as source_rank,
       icao24, start_time as win_start, end_time as win_end, callsign, origin_icao, dest_icao
from {{ ref('int_flight_legs_opensky') }}
where icao24 is not null and start_time is not null and end_time is not null
