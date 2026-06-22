-- Derives from fct_flight_legs (OpenSky context feed), so it's untagged and built by transform_marts alongside it.
{{ config(materialized='table') }}

-- Top INFERRED routes for the deck.gl arc map; both endpoints resolved only (see fct_flight_legs.route_inferred).
-- Exclude origin==dest: single-fix / local legs snap to one airport and are degenerate zero-length arcs.
select
    route_inferred,
    origin_icao,
    origin_name,
    origin_lat,
    origin_lon,
    dest_icao,
    dest_name,
    dest_lat,
    dest_lon,
    count(*)               as leg_count,
    count(distinct icao24) as distinct_aircraft,
    max(end_time)          as last_seen
from {{ ref('fct_flight_legs') }}
where route_inferred is not null
  and origin_icao != dest_icao
group by 1, 2, 3, 4, 5, 6, 7, 8, 9
order by leg_count desc
