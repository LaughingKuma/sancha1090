{{ config(materialized='table') }}

-- Skip-reason / segment-kind / bin distributions per day (ledger item 1). Long format:
-- one (dimension, value) row family per population, cheap to chart and to extend.
select day, producer, 'skip' as dimension, concat(s.1, ':', s.2) as value, count() as n
from (
    select toDate(computed_at) as day, producer, skips
    from {{ source('bronze', 'path_estimates') }}
    where producer != 'test' and seg_idx = 0
)
array join skips as s
group by day, producer, value

union all

select day, producer, 'segment_kind' as dimension, kind as value, count() as n
from (
    select toDate(computed_at) as day, producer, kind
    from {{ source('bronze', 'path_estimates') }}
    where producer != 'test' and seg_idx > 0
)
group by day, producer, value

union all

select day, producer, 'uncertainty_bin' as dimension, uncertainty_bin as value, count() as n
from (
    select toDate(computed_at) as day, producer, uncertainty_bin
    from {{ source('bronze', 'path_estimates') }}
    where producer != 'test' and seg_idx > 0
)
group by day, producer, value
