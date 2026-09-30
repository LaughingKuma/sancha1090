{{ config(tags=['adsb']) }}
-- Fails if any day lost or doubled fixes against bronze; both sides read without FINAL.
select d as day, sumIf(n, src = 'bronze') as bronze_rows, sumIf(n, src = 'rollup') as rollup_rows
from (
    select capture_date as d, count() as n, 'bronze' as src
    from {{ source('bronze', 'adsb_states') }}
    where hex is not null and capture_ts is not null
    group by capture_date
    union all
    select capture_date, sum(n_fixes), 'rollup'
    from {{ ref('agg_rooftop_airframe_hour') }}
    group by capture_date
)
group by d
having bronze_rows != rollup_rows
