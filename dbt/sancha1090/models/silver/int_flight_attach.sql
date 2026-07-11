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
                     sp.anchor_rank asc, sp.flight_id asc, o.origin_icao asc, o.dest_icao asc
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
        -- Total-order tiebreak (origin/dest last): attached is referenced twice and re-evaluated, so a
        -- duplicate same-window opinion (e.g. a round-trip filed both directions) must resolve identically.
        row_number() over (
            partition by flight_id, source
            -- opensky_flights near-dups: most-resolved first (a merged near-dup must not vote with a wide
            -- NULL-endpoint capture). Other sources keep SP1's overlap-first (resolvedness term = 0, inert).
            order by multiIf(source = 'opensky_flights',
                             toInt8(if(origin_icao is not null, 1, 0) + if(dest_icao is not null, 1, 0)),
                             toInt8(0)) desc,
                     overlap_s desc, win_start asc, origin_icao asc, dest_icao asc
        ) as source_rn
    from cand
    where rn = 1
),
-- SP4 + Finding 3: vrs_routes votes but never anchors (standing data has no window). Multi-hop schedules
-- explode into box-gated legs; a lone box-leg votes unconditionally, a multi-leg schedule votes only the
-- leg its own observed endpoints uniquely corroborate (else abstains). One vote/flight; jet-gated as usual.
vrs_cand as (
    select
        sp.flight_id as flight_id,
        sp.icao24 as icao24,
        sp.anchor_callsign as anchor_callsign,
        v.origin_icao as origin_icao, v.dest_icao as dest_icao,
        v.n_box_legs as n_box_legs
    from {{ ref('int_flight_spine') }} sp
    join {{ ref('stg_vrs_routes') }} v on v.callsign_norm = {{ callsign_norm('sp.anchor_callsign') }}
),
-- Score each candidate leg by position-aligned corroboration against the flight's own resolved votes:
-- an observed dest matching a leg's origin is NOT support (that flight ended where the next leg starts).
vrs_scored as (
    select
        c.flight_id as flight_id, c.icao24 as icao24, c.anchor_callsign as anchor_callsign,
        c.origin_icao as origin_icao, c.dest_icao as dest_icao, c.n_box_legs as n_box_legs,
        countIf(a.origin_icao = c.origin_icao) + countIf(a.dest_icao = c.dest_icao) as support
    from vrs_cand c
    left join (select flight_id, origin_icao, dest_icao from attached where source_rn = 1) a
           on a.flight_id = c.flight_id
    group by c.flight_id, c.icao24, c.anchor_callsign, c.origin_icao, c.dest_icao, c.n_box_legs
),
vrs_pick as (
    select *, sum(if(support > 0, 1, 0)) over (partition by flight_id) as n_supported
    from vrs_scored
),
vrs_votes as (
    select
        p.flight_id as flight_id,
        'vrs_routes' as source, toUInt8(2) as source_rank,
        if({{ jet_infeasible_endpoint(airline_shaped('p.anchor_callsign'), 'j.icao24 is not null', 'oa.runway_length_ft', 'oa.airport_type') }},
           NULL, p.origin_icao) as origin_icao,
        if({{ jet_infeasible_endpoint(airline_shaped('p.anchor_callsign'), 'j.icao24 is not null', 'da.runway_length_ft', 'da.airport_type') }},
           NULL, p.dest_icao) as dest_icao
    from vrs_pick p
    left join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(p.icao24)
    left join {{ ref('dim_airports') }} oa on oa.icao = p.origin_icao
    left join {{ ref('dim_airports') }} da on da.icao = p.dest_icao
    where p.n_box_legs = 1 or (p.support > 0 and p.n_supported = 1)
)
select flight_id, source, source_rank, origin_icao, dest_icao
from attached
where source_rn = 1
union all
select flight_id, source, source_rank, origin_icao, dest_icao
from vrs_votes
where origin_icao is not null or dest_icao is not null
