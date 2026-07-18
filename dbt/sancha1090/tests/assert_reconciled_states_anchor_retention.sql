-- Erosion tripwire: a rolling filter upstream of the spine evaporates states-anchored flights oldest-first;
-- a well-covered day with zero of them is the first symptom (2026-07-18 horizon-erosion incident).
-- Coverage = in-box positioned rows (the staging population), >= 30k (~8h of normal ~90k/day density):
-- separates erosion (full-data day, zero anchors) from genuinely sparse/partial/outage days.
with bronze_days as (
    select snapshot_date as d
    from {{ source('bronze', 'opensky_states') }}
    where {{ in_japan_box('latitude', 'longitude') }}
      and snapshot_date >= (select min(snapshot_date) + 2 from {{ source('bronze', 'opensky_states') }})
      and snapshot_date <= toDate(now('UTC')) - 1
    group by snapshot_date
    having count() >= 30000
),
anchored as (
    select toDate(start_time) as d, count() as n
    from {{ ref('fct_flights_reconciled') }}
    where anchor_source = 'opensky_states'
    group by d
)
select b.d as day_missing_states_anchor
from bronze_days b
left join anchored a on a.d = b.d
where coalesce(a.n, 0) = 0
