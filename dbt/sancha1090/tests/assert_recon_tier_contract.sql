-- Full contract pin: re-derive every mart row independently (row presence both directions, all four
-- tiers including provisional/none, effective_gap_s including NULL-for-pathless) and fail on any drift.
with head as (
    select coalesce(max(day_key), toDate('1970-01-01')) as head_day
    from {{ ref('fct_flight_path') }}
),
expected as (
    select
        r.flight_id as flight_id,
        if(s.flight_id is not null,
           greatest(
               coalesce(s.largest_gap_s, 0),
               if(s.first_fix_ts > r.start_time, dateDiff('second', r.start_time, s.first_fix_ts), 0),
               if(s.last_fix_ts < r.end_time, dateDiff('second', s.last_fix_ts, r.end_time), 0)),
           NULL) as eff,
        multiIf(
            s.flight_id is not null and eff <= {{ var('recon_tier_gap_s') }}, 'settled',
            s.flight_id is not null, 'estimated',
            r.start_time is not null and toDate(r.start_time) > (select head_day from head), 'provisional',
            'none') as tier
    from {{ ref('fct_flights_reconciled') }} r
    left join {{ ref('fct_flight_path_summary') }} s on s.flight_id = r.flight_id
    where r.flight_id is not null
)
select coalesce(e.flight_id, t.flight_id) as flight_id, e.tier as expected_tier, t.tier as mart_tier
from expected e
full outer join {{ ref('fct_flight_recon_tier') }} t on t.flight_id = e.flight_id
where e.flight_id is null
   or t.flight_id is null
   or e.tier != t.tier
   or (e.eff is null) != (t.effective_gap_s is null)
   or coalesce(e.eff, 0) != coalesce(t.effective_gap_s, 0)
