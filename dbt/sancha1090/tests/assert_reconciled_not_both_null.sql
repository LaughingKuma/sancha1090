select sp.flight_id
from {{ ref('int_flight_spine') }} sp
join {{ ref('int_flight_attach') }} a on a.flight_id = sp.flight_id
join {{ ref('fct_flights_reconciled') }} r on r.flight_id = sp.flight_id
where (a.origin_icao is not null or a.dest_icao is not null)
group by sp.flight_id
having max(if(r.origin_icao is not null or r.dest_icao is not null, 1, 0)) = 0
