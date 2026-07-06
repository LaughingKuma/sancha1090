{{ config(materialized='table', tags=['reconcile']) }}

-- Attach every opinion to its best-overlap spine anchor (callsign-guarded), so a spanning record
-- attaches to one flight rather than merging two. One row per (flight_id, source): a source's vote.
with cand as (
    select
        sp.flight_id as flight_id,
        o.source as source, o.source_rank as source_rank,
        o.origin_icao as origin_icao, o.dest_icao as dest_icao,
        o.icao24 as icao24, o.win_start as win_start, o.win_end as win_end,
        dateDiff('second', greatest(o.win_start, sp.flight_start), least(o.win_end, sp.flight_end)) as overlap_s,
        row_number() over (
            partition by o.source, o.icao24, o.win_start
            order by dateDiff('second', greatest(o.win_start, sp.flight_start), least(o.win_end, sp.flight_end)) desc,
                     sp.anchor_rank asc, sp.flight_id asc
        ) as rn
    from {{ ref('int_flight_opinions') }} o
    join {{ ref('int_flight_spine') }} sp on sp.icao24 = o.icao24
    where sp.flight_start <= o.win_end and sp.flight_end >= o.win_start
      and (o.callsign = sp.anchor_callsign or o.callsign is null or sp.anchor_callsign is null)
),
-- A fragmented source (e.g. adsblol's unchained short segments) can independently max-overlap the
-- same anchor from several opinions; collapse to one vote per (flight_id, source), best overlap wins.
attached as (
    select
        flight_id, source, source_rank, origin_icao, dest_icao, overlap_s, win_start,
        row_number() over (
            partition by flight_id, source
            order by overlap_s desc, win_start asc
        ) as source_rn
    from cand
    where rn = 1
)
select flight_id, source, source_rank, origin_icao, dest_icao
from attached
where source_rn = 1
