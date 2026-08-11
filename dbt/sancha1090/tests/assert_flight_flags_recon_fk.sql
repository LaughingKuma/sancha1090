-- flight_id churns at settlement/repair; a flag left pointing at a dead id sends the anomaly feed to
-- an empty focus panel instead of erroring.
select f.flight_id, count() as orphan_rows
from {{ ref('fct_flight_flags') }} f
left anti join {{ ref('fct_flights_reconciled') }} r on r.flight_id = f.flight_id
group by f.flight_id
