{{ config(materialized='table', tags=['adsblol', 'ch_mv']) }}

-- Pre-pipeline hourly aggregates at the exact agg_hourly_traffic grain, so the
-- live mart can UNION ALL without Superset noticing a new dataset.
select
    toStartOfHour(snapshot_time)            as snapshot_hour,
    {{ hourly_traffic_measures() }}
from {{ ref('stg_states_adsblol') }}
where latitude is not null
  and longitude is not null
group by 1
