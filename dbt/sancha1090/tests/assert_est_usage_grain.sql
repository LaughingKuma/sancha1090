-- one row per (day, producer, arm) — a duplicate grain would double-count the gate query
select day, producer, arm
from {{ ref('agg_est_usage_daily') }}
group by day, producer, arm
having count() > 1
