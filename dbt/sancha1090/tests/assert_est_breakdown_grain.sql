select day, producer, dimension, value
from {{ ref('agg_est_breakdown_daily') }}
group by day, producer, dimension, value
having count() > 1
