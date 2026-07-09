-- SP4: standing data must contribute at most one vote pair per flight (a dup would double-weight the
-- schedule prior against observation).
select flight_id, count() as votes
from {{ ref('int_flight_attach') }}
where source = 'vrs_routes'
group by flight_id
having count() > 1
