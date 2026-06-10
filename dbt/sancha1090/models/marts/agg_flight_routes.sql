{{ config(materialized='table', tags=['flights']) }}

-- Ground-truth top routes from flight summaries — the counterpart to the INFERRED
-- agg_route_traffic (state-vector sessionization), which stays untouched for the arc map.
select
    origin_icao || ' → ' || dest_icao as route,
    origin_icao,
    origin_iata,
    origin_city,
    origin_lat,
    origin_lon,
    dest_icao,
    dest_iata,
    dest_city,
    dest_lat,
    dest_lon,
    count(*)               as flight_count,
    count(distinct icao24) as distinct_aircraft,
    max(first_seen)        as last_flight
from {{ ref('fact_flights') }}
where origin_icao is not null
  and dest_icao is not null
  and origin_icao != dest_icao
  and seen_in_context
group by 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
order by flight_count desc
