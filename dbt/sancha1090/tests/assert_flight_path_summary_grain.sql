-- Grain: one summary row per flight_id.
select flight_id
from {{ ref('fct_flight_path_summary') }}
group by flight_id
having count(*) > 1
