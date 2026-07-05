-- Hand-entered override ICAOs must resolve in dim_airports, or fct emits a "resolved"
-- endpoint with NULL name/coords (curated legs would silently lose their map data).
select o.callsign, o.valid_from
from {{ ref('dim_route_overrides') }} o
left join {{ ref('dim_airports') }} oa on oa.icao = nullIf(o.origin_icao, '')
left join {{ ref('dim_airports') }} da on da.icao = nullIf(o.dest_icao, '')
where (nullIf(o.origin_icao, '') is not null and oa.icao is null)
   or (nullIf(o.dest_icao, '') is not null and da.icao is null)
