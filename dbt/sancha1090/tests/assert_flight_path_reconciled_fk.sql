-- A replaced daily partition must contain only ids from the current spine; stale ids make /path silently empty.
select p.flight_id, count() as orphan_rows
from {{ ref('fct_flight_path') }} p
left anti join {{ ref('fct_flights_reconciled') }} r on r.flight_id = p.flight_id
group by p.flight_id
