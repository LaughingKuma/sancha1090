-- A broken predicate (renamed upstream column, inverted gate) empties one class silently: every class
-- is populated at any realistic warehouse size, so a zero-row class is the failure signal.
with classes as (
    select arrayJoin(['tiebreak_endpoint', 'single_source', 'one_sided_intl',
                      'feasibility_snap', 'diversion', 'same_endpoint', 'military']) as flag_class
)
select c.flag_class as empty_class
from classes c
left anti join (select distinct flag_class from {{ ref('fct_flight_flags') }}) m on m.flag_class = c.flag_class
-- A rebuilding warehouse needs history, not just rows (measured: 1,000 rows on day 2, diversion on day 11).
-- 30 clears that floor and the rolling ~90d horizon; gating on the window length would ride the edge.
where (select count() from {{ ref('fct_flights_reconciled') }}) >= 1000
  and (select dateDiff('day', min(start_time), max(start_time)) from {{ ref('fct_flights_reconciled') }}) >= 30
