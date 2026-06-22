{{ config(materialized='table', tags=['history', 'ch_mv']) }}

-- Pre-pipeline hourly aggregates at the exact agg_hourly_traffic grain, so the
-- live mart can UNION ALL without Superset noticing a new dataset.
select
    {% if target.type == 'clickhouse' %}toStartOfHour(snapshot_time){% else %}date_trunc('hour', snapshot_time){% endif %}            as snapshot_hour,
    {{ hourly_traffic_measures() }}
from {{ ref('stg_states_history') }}
where latitude is not null
  and longitude is not null
group by 1
