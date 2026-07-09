-- D1: no reconciled flight may span longer than the anchor cap (a fused multi-leg rotation).
select flight_id, anchor_source, round((toUnixTimestamp(end_time) - toUnixTimestamp(start_time)) / 3600.0, 2) as hrs
from {{ ref('fct_flights_reconciled') }}
where (toUnixTimestamp(end_time) - toUnixTimestamp(start_time)) / 3600.0 > {{ var('reconcile_anchor_max_hours') }}
