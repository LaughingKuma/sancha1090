-- Grain: at most one fused fix per (flight_id, ts).
select flight_id, ts
from {{ ref('fct_flight_path') }}
group by flight_id, ts
having count(*) > 1
