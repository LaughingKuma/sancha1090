{{ config(materialized='table', tags=['reconcile']) }}

-- Flight spine: one row per authority-ranked flight anchor. Authority-ranked anchoring -- opensky_flights first,
-- adsb.lol only where it overlaps no callsign-compatible flight-summary, opensky_states last.
-- A collapsed low-authority record can never anchor when a cleaner source exists, so it can never
-- merge two flights. CH ON is equi-only -> overlap anti-join via equi(icao24)+HAVING count=0.
with op_flights_raw as (
    select icao24, win_start, win_end, callsign from {{ ref('int_flight_opinions') }} where source = 'opensky_flights'
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
    -- stage 2: within a gap island, break on a non-null callsign that differs from the island's ESTABLISHED
    -- callsign (anyLast ignores NULLs -> last non-null so far IN THIS island, so a NULL row can't bridge
    -- A→NULL→B). Every gap-island boundary is also a break (a big time gap = new flight even if callsign repeats).
    select *,
        case
            when gap_break = 1 then 1
            when prev_cs is not null and callsign is not null
                 and trimBoth(callsign) != trimBoth(prev_cs) then 1
            else 0
        end as cluster_break
    from (
        select *,
            anyLast(callsign) over (partition by icao24, gap_group order by win_start, win_end
                                    rows between unbounded preceding and 1 preceding) as prev_cs
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
op_flights as (
    -- one merged anchor per cluster: widest window, earliest non-null callsign (tuple() preserves NULL,
    -- isNull ordering sorts non-null first so a resolved callsign wins over a blank).
    select icao24,
        min(win_start) as win_start,
        max(win_end) as win_end,
        argMin(tuple(callsign), tuple(isNull(callsign), ws_raw)).1 as callsign
    from op_clustered
    group by icao24, cluster_id
),
adsblol as (
    select icao24, win_start, win_end, callsign from {{ ref('int_flight_opinions') }} where source = 'adsblol'
),
opensky_states as (
    select icao24, win_start, win_end, callsign from {{ ref('int_flight_opinions') }} where source = 'opensky_states'
),
adsblol_anchors as (
    select a.icao24 as icao24, a.win_start as win_start, a.win_end as win_end, a.callsign as callsign
    from adsblol a
    left join op_flights f on f.icao24 = a.icao24
    group by a.icao24, a.win_start, a.win_end, a.callsign
    having sum(if(f.icao24 is not null
                  and f.win_start <= a.win_end and f.win_end >= a.win_start
                  and (f.callsign = a.callsign or f.callsign is null or a.callsign is null), 1, 0)) = 0
),
higher as (
    select icao24, win_start, win_end, callsign from op_flights
    union all
    select icao24, win_start, win_end, callsign from adsblol_anchors
),
states_anchors as (
    select a.icao24 as icao24, a.win_start as win_start, a.win_end as win_end, a.callsign as callsign
    from opensky_states a
    left join higher h on h.icao24 = a.icao24
    where (toUnixTimestamp(a.win_end) - toUnixTimestamp(a.win_start)) / 3600.0 <= {{ var('flight_max_hours') }}  -- D4.2: drop fused rotations
    group by a.icao24, a.win_start, a.win_end, a.callsign
    having sum(if(h.icao24 is not null
                  and h.win_start <= a.win_end and h.win_end >= a.win_start
                  and (h.callsign = a.callsign or h.callsign is null or a.callsign is null), 1, 0)) = 0
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
