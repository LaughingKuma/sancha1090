{{ config(
    materialized='table',
    tags=['reconcile'],
    query_settings={'max_memory_usage': 8000000000},
) }}

-- The day key bounds the equi-join before exact interval/callsign filtering; a spanning pair can repeat
-- across days, but both argMin stages are idempotent for those identical candidates.
with opinions_by_day as (
    select
        *,
        {{ overlap_days('win_start', 'win_end') }} as overlap_day
    from {{ ref('int_flight_opinions') }}
    where win_start is not null and win_end is not null
),
spine_by_day as (
    select
        *,
        {{ overlap_days('flight_start', 'flight_end') }} as overlap_day
    from {{ ref('int_flight_spine') }}
    where flight_start is not null and flight_end is not null
),
-- Attach every opinion to its best-overlap spine anchor (callsign-guarded), so a spanning record
-- attaches to one flight rather than merging two.
cand as (
    select
        sp.flight_id as flight_id,
        sp.anchor_rank as anchor_rank,
        o.source as source,
        o.source_rank as source_rank,
        o.origin_icao as origin_icao,
        o.dest_icao as dest_icao,
        o.origin_gated as origin_gated,
        o.dest_gated as dest_gated,
        o.icao24 as icao24,
        o.win_start as win_start,
        dateDiff(
            'second', greatest(o.win_start, sp.flight_start), least(o.win_end, sp.flight_end)
        ) as overlap_s
    from opinions_by_day o
    join spine_by_day sp on sp.icao24 = o.icao24 and sp.overlap_day = o.overlap_day
    where sp.flight_start <= o.win_end and sp.flight_end >= o.win_start
      and (o.callsign = sp.anchor_callsign or o.callsign is null or sp.anchor_callsign is null)
),
-- Best anchor per opinion window. Origin/dest enter the key as (isNull, ifNull) pairs so NULL sorts last
-- as in ORDER BY ASC; the value is tuple()-wrapped so a NULL endpoint can't make argMin skip the row.
anchor_pick as (
    select
        source,
        icao24,
        win_start,
        argMin(
            tuple(flight_id, source_rank, origin_icao, dest_icao, origin_gated, dest_gated, overlap_s),
            tuple(
                -overlap_s,
                anchor_rank,
                flight_id,
                isNull(origin_icao), ifNull(origin_icao, ''),
                isNull(dest_icao), ifNull(dest_icao, '')
            )
        ) as picked
    from cand
    group by source, icao24, win_start
),
-- Several opinions from one source can pick the same anchor, so only the best-overlap opinion gets a vote.
-- Its (icao24, win_start) key lets consumers find the opinion that cast the vote.
votes as (
    select
        picked.1 as flight_id,
        source,
        icao24,
        argMin(
            tuple(picked.2, picked.3, picked.4, win_start),
            tuple(
                -- opensky_flights near-dups: most-resolved first, so a merged near-dup never votes with a wide
                -- NULL-endpoint capture. Inert (0) for every other source: they keep overlap-first.
                -multiIf(
                    source = 'opensky_flights',
                    toInt8(if(picked.3 is not null, 1, 0) + if(picked.4 is not null, 1, 0)),
                    toInt8(0)
                ),
                -picked.7,
                win_start,
                isNull(picked.3), ifNull(picked.3, ''),
                isNull(picked.4), ifNull(picked.4, '')
            )
        ) as vote,
        -- Gate flags over ALL the source's window winners, not just the vote: the resolvedness tiebreak prefers
        -- un-NULLed rows, so reading the flags off the winning row alone would hide the gate hit.
        max(picked.5) as src_origin_gated,
        max(picked.6) as src_dest_gated
    from anchor_pick
    group by flight_id, source, icao24
)
select
    flight_id,
    source,
    vote.1 as source_rank,
    vote.2 as origin_icao,
    vote.3 as dest_icao,
    src_origin_gated as origin_gated,
    src_dest_gated as dest_gated,
    icao24,
    vote.4 as win_start
from votes
