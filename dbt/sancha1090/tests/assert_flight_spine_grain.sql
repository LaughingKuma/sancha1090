select flight_id
from {{ ref('int_flight_spine') }}
group by flight_id
having count(*) > 1 or uniqExact(icao24) > 1 or uniqExact(flight_start) > 1
