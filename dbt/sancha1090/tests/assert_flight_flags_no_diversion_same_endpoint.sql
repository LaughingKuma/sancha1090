-- The de-overlap predicate is one deletable line in the model; this pins that origin=dest rows
-- are carried by same_endpoint alone, not double-filed under diversion too.
select f.flight_id
from {{ ref('fct_flight_flags') }} f
join {{ ref('fct_flights_reconciled') }} r on r.flight_id = f.flight_id
where f.flag_class = 'diversion'
  and r.origin_icao = r.dest_icao
