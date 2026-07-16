{{ config(materialized='table', query_settings={'max_memory_usage': 16000000000}) }}

-- Per-flight QA row for fct_flight_path: point counts, per-source split, largest coverage gap, and
-- observed_fraction over the reconciled window -- makes honest-gap labelling queryable without scanning the
-- point table. Inner join reconciled: orphaned flight_ids (historical re-key) drop out. n_points/n_*/largest_
-- gap_s are whole-recorded-path (they keep the +/-10min pad fixes); observed_fraction's numerator counts only
-- fixes inside the UNPADDED [start_time, end_time] so it is not inflated by pad fixes over the unpadded window_s.
with pts as (
    select p.flight_id, p.ts, p.source, r.start_time, r.end_time
    from {{ ref('fct_flight_path') }} p
    join {{ ref('fct_flights_reconciled') }} r on r.flight_id = p.flight_id
),
agg as (
    -- start_time/end_time are constant per flight_id -> group by them too so they're usable (not aggregates)
    -- in the in-window countIf and window_s.
    select
        flight_id,
        count() as n_points,
        countIf(source = 'adsb') as n_adsb,
        countIf(source = 'adsblol') as n_adsblol,
        countIf(source = 'opensky') as n_opensky,
        min(ts) as first_fix_ts,
        max(ts) as last_fix_ts,
        -- consecutive-ts deltas over the sorted second-grain fixes; single-point flights yield 0.
        toUInt32(arrayMax(arrayDifference(arraySort(groupArray(toUnixTimestamp(ts)))))) as largest_gap_s,
        countIf(ts between start_time and end_time) as n_in_window,
        toUInt32(dateDiff('second', start_time, end_time)) as window_s
    from pts
    group by flight_id, start_time, end_time
)
select
    flight_id,
    n_points,
    n_adsb,
    n_adsblol,
    n_opensky,
    first_fix_ts,
    last_fix_ts,
    largest_gap_s,
    window_s,
    if(window_s = 0, toFloat64(0), least(toFloat64(1), n_in_window / window_s)) as observed_fraction
from agg
