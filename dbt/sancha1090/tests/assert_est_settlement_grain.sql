{{ config(severity='warn') }}
-- one row per (estimate_id, seg_idx); warn-only — telemetry must never red the ~4-min
-- serving transform tick (push_flight_routes depends on dbt_test_ch)
select estimate_id, seg_idx
from {{ ref('fct_est_settlement') }}
group by estimate_id, seg_idx
having count() > 1
