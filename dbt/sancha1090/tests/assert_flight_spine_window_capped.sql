-- D4.2: no anchor from a capped source spans more than flight_max_hours (a fused rotation).
select flight_id, anchor_source, round((toUnixTimestamp(flight_end) - toUnixTimestamp(flight_start)) / 3600.0, 2) as hrs
from {{ ref('int_flight_spine') }}
where anchor_source in ('opensky_states')
  and (toUnixTimestamp(flight_end) - toUnixTimestamp(flight_start)) / 3600.0 > {{ var('flight_max_hours') }}
