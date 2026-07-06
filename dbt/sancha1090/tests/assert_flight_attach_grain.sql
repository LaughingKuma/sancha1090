select flight_id, source
from {{ ref('int_flight_attach') }}
group by flight_id, source
having count(*) > 1
