-- Pad hardcoded (not var('path_window_pad_min')) so a model-side pad regression surfaces here instead of
-- flowing through the shared var and passing tautologically. Keep in sync with the var in dbt_project.yml (10).
select p.flight_id, p.ts
from {{ ref('fct_flight_path') }} p
join {{ ref('fct_flights_reconciled') }} r on r.flight_id = p.flight_id
where p.ts < toDateTime(r.start_time, 'UTC') - interval 10 minute
   or p.ts > toDateTime(r.end_time,   'UTC') + interval 10 minute
