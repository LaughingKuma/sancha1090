-- One consensus-route mart (SP2): replaces the split inferred(agg_route_traffic←legs) +
-- authoritative(agg_flight_routes←flights) lanes. tag:reconcile so it builds in transform_marts
-- after fct_flights_reconciled. Geo comes straight off the enriched mart -- no dim_airports join.
{{ config(materialized='table', tags=['reconcile']) }}

select
    concat(origin_icao, '-', dest_icao) as route_inferred,
    origin_icao,
    origin_name,
    origin_lat,
    origin_lon,
    dest_icao,
    dest_name,
    dest_lat,
    dest_lon,
    count(*)               as flight_count,
    count(distinct icao24) as distinct_aircraft,
    max(end_time)          as last_seen
from {{ ref('fct_flights_reconciled') }}
where origin_icao is not null
  and dest_icao is not null
  and origin_icao != dest_icao
group by 1, 2, 3, 4, 5, 6, 7, 8, 9
order by flight_count desc
