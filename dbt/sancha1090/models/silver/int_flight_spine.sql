{{ config(
    materialized='table',
    tags=['reconcile'],
    query_settings={'max_memory_usage': 4000000000},
) }}

-- Flight spine: one row per authority-ranked flight anchor. Authority-ranked anchoring -- opensky_flights first,
-- adsb.lol only where it overlaps no callsign-compatible flight-summary, opensky_states last.
-- A collapsed low-authority record can never anchor when a cleaner source exists, so it can never
-- merge two flights. CH ON is equi-only -> overlap anti-join via equi(icao24)+HAVING count=0.
with op_flights_raw as (
    select icao24, win_start, win_end, callsign, origin_icao, dest_icao from {{ ref('int_flight_opinions') }} where source = 'opensky_flights'
),
op_gapped as (
    -- stage 1: gap-only islands. A gap island = a run with no >anchor_merge_gap_min break from the running
    -- max end. The callsign carry-forward in stage 2 is partitioned BY this island so it resets at each gap
    -- (else A→gap→NULL→B would compare B against the pre-gap A and wrongly split B from its NULL-started run).
    select *,
        sum(gap_break) over (partition by icao24 order by win_start, win_end
                             rows between unbounded preceding and current row) as gap_group
    from (
        select *,
            case when max_prev_end is null
                      or win_start > max_prev_end + interval {{ var('anchor_merge_gap_min') }} minute
                 then 1 else 0 end as gap_break
        from (
            select *,
                max(win_end) over (partition by icao24 order by win_start, win_end
                                   rows between unbounded preceding and 1 preceding) as max_prev_end
            from op_flights_raw
        )
    )
),
op_flagged as (
    -- stage 2: break on a differing ESTABLISHED callsign (anyLast skips NULLs: A→NULL→B can't bridge), a
    -- gap-island boundary, or a landing then sequential same-field departure (#150 tail-number turnarounds).
    select *,
        case
            when gap_break = 1 then 1
            when prev_dest is not null and origin_icao is not null and prev_dest = origin_icao
                 and prev_win_end is not null and win_start >= prev_win_end then 1
            when prev_cs is not null and callsign is not null
                 and trimBoth(callsign) != trimBoth(prev_cs) then 1
            else 0
        end as cluster_break
    from (
        select *,
            anyLast(callsign) over (partition by icao24, gap_group order by win_start, win_end
                                    rows between unbounded preceding and 1 preceding) as prev_cs,
            lagInFrame(dest_icao, 1, NULL) over (partition by icao24, gap_group order by win_start, win_end
                                    rows between 1 preceding and current row) as prev_dest,
            lagInFrame(toNullable(win_end), 1, NULL) over (partition by icao24, gap_group order by win_start, win_end
                                    rows between 1 preceding and current row) as prev_win_end
        from op_gapped
    )
),
op_clustered as (
    select *,
        -- ws_raw exposes the RAW per-row win_start under a distinct name. argMin below MUST use ws_raw, not
        -- `win_start` — the latter binds to the min(win_start) output alias, giving aggregate-inside-aggregate
        -- (ClickHouse Code 184 ILLEGAL_AGGREGATION; reproduced on live CH 26.5.1, aborts the first dbt run).
        win_start as ws_raw,
        sum(cluster_break) over (partition by icao24 order by win_start, win_end
                                 rows between unbounded preceding and current row) as cluster_id
    from op_flagged
),
op_merged as (
    -- one merged anchor per cluster: widest window, earliest non-null callsign (tuple() preserves NULL,
    -- isNull ordering sorts non-null first so a resolved callsign wins over a blank).
    select icao24,
        min(win_start) as win_start,
        max(win_end) as win_end,
        argMin(tuple(callsign), tuple(isNull(callsign), ws_raw)).1 as callsign
    from op_clustered
    group by icao24, cluster_id
),
-- cs_junk/cs_num are precomputed per row, never inside the anti-join predicates: those run once per
-- candidate PAIR, and the compat regexes would then be evaluated over the whole overlap cross-product.
op_flights as (
    select icao24, win_start, win_end, callsign,
        {{ callsign_is_junk('callsign') }} as cs_junk,
        {{ callsign_flight_num('callsign') }} as cs_num
    from op_merged
),
adsblol as (
    select icao24, win_start, win_end, callsign,
        {{ callsign_is_junk('callsign') }} as cs_junk,
        {{ callsign_flight_num('callsign') }} as cs_num
    from {{ ref('int_flight_opinions') }} where source = 'adsblol'
),
opensky_states as (
    select icao24, win_start, win_end, callsign,
        {{ callsign_is_junk('callsign') }} as cs_junk,
        {{ callsign_flight_num('callsign') }} as cs_num
    from {{ ref('int_flight_opinions') }} where source = 'opensky_states'
),
-- The HAVING sums are day-dup safe: an overlapping pair shares >= 1 day so it still lands non-zero,
-- and a same-day non-overlapping pair still contributes 0.
adsblol_by_day as (
    select *, {{ overlap_days('win_start', 'win_end') }} as overlap_day
    from adsblol
    where win_start is not null and win_end is not null
),
op_flights_by_day as (
    select *, {{ overlap_days('win_start', 'win_end') }} as overlap_day
    from op_flights
    where win_start is not null and win_end is not null
),
adsblol_anchors as (
    -- any() is exact here: cs_junk/cs_num are functions of the group key callsign.
    select a.icao24 as icao24, a.win_start as win_start, a.win_end as win_end, a.callsign as callsign,
           any(a.cs_junk) as cs_junk, any(a.cs_num) as cs_num
    from adsblol_by_day a
    left join op_flights_by_day f on f.icao24 = a.icao24 and f.overlap_day = a.overlap_day
    group by a.icao24, a.win_start, a.win_end, a.callsign
    -- 5C: the same flight number on the same airframe in overlapping windows is one flight however the
    -- prefix is spelled (JL45/JAL45); a digit difference never waives, it is the leg-splitting guard.
    having sum(if(f.icao24 is not null
                  and f.win_start <= a.win_end and f.win_end >= a.win_start
                  and (f.callsign = a.callsign or f.cs_junk or a.cs_junk
                       or (f.cs_num is not null and f.cs_num = a.cs_num)), 1, 0)) = 0
),
higher as (
    select icao24, win_start, win_end, callsign, cs_junk, cs_num from op_flights
    union all
    select icao24, win_start, win_end, callsign, cs_junk, cs_num from adsblol_anchors
),
states_by_day as (
    select *, {{ overlap_days('win_start', 'win_end') }} as overlap_day
    from opensky_states
    where win_start is not null and win_end is not null
),
higher_by_day as (
    select *, {{ overlap_days('win_start', 'win_end') }} as overlap_day
    from higher
    where win_start is not null and win_end is not null
),
states_anchors as (
    select a.icao24 as icao24, a.win_start as win_start, a.win_end as win_end, a.callsign as callsign
    from states_by_day a
    left join higher_by_day h on h.icao24 = a.icao24 and h.overlap_day = a.overlap_day
    where (toUnixTimestamp(a.win_end) - toUnixTimestamp(a.win_start)) / 3600.0 <= {{ var('flight_max_hours') }}  -- D4.2: drop fused rotations
    group by a.icao24, a.win_start, a.win_end, a.callsign
    -- 5D: a sub-minute ping nested in another flight's window is that flight regardless of what its
    -- garbled callsign says, so the callsign-compat test is waived for degenerate anchors.
    having sum(if(h.icao24 is not null
                  and h.win_start <= a.win_end and h.win_end >= a.win_start
                  and (h.callsign = a.callsign or h.cs_junk or a.cs_junk
                       or (h.cs_num is not null and h.cs_num = a.cs_num)
                       or dateDiff('second', a.win_start, a.win_end) < {{ var('micro_anchor_max_s') }}), 1, 0)) = 0
),
all_anchors as (
    select icao24, win_start, win_end, callsign, 'opensky_flights' as anchor_source, toUInt8(1) as anchor_rank from op_flights
    union all
    select icao24, win_start, win_end, callsign, 'adsblol' as anchor_source, toUInt8(2) as anchor_rank from adsblol_anchors
    union all
    select icao24, win_start, win_end, callsign, 'opensky_states' as anchor_source, toUInt8(3) as anchor_rank from states_anchors
)
select
    cityHash64(icao24, toString(win_start), anchor_source) as flight_id,
    icao24,
    win_start as flight_start,
    win_end   as flight_end,
    callsign  as anchor_callsign,
    anchor_source,
    anchor_rank
from all_anchors
