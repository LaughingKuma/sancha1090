-- D4.2: no anchor from a capped source spans more than flight_max_hours (a fused rotation).
select flight_id, anchor_source, dateDiff('hour', flight_start, flight_end) as hrs
from {{ ref('int_flight_spine') }}
where anchor_source in ('opensky_states')
  and dateDiff('hour', flight_start, flight_end) > {{ var('flight_max_hours') }}
