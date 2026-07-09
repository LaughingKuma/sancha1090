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
            -- opensky_flights near-dups: most-resolved first (a merged near-dup must not vote with a wide
            -- NULL-endpoint capture). Other sources keep SP1's overlap-first (resolvedness term = 0, inert).
            order by multiIf(source = 'opensky_flights',
                             toInt8(if(origin_icao is not null, 1, 0) + if(dest_icao is not null, 1, 0)),
                             toInt8(0)) desc,
                     overlap_s desc, win_start asc
        ) as source_rn
    from cand
    where rn = 1
),
-- SP4: vrs_routes votes but never anchors (standing data has no window). Exactly one vote per flight,
-- keyed on the anchor's normalized callsign; jet-infeasible endpoints gated like every other opinion.
vrs_votes as (
    select
        sp.flight_id as flight_id,
        'vrs_routes' as source, toUInt8(2) as source_rank,
        if({{ jet_infeasible_endpoint(airline_shaped('sp.anchor_callsign'), 'j.icao24 is not null', 'oa.runway_length_ft', 'oa.airport_type') }},
           NULL, v.origin_icao) as origin_icao,
        if({{ jet_infeasible_endpoint(airline_shaped('sp.anchor_callsign'), 'j.icao24 is not null', 'da.runway_length_ft', 'da.airport_type') }},
           NULL, v.dest_icao) as dest_icao
    from {{ ref('int_flight_spine') }} sp
    join {{ ref('stg_vrs_routes') }} v on v.callsign_norm = {{ callsign_norm('sp.anchor_callsign') }}
    left join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(sp.icao24)
    left join {{ ref('dim_airports') }} oa on oa.icao = v.origin_icao
    left join {{ ref('dim_airports') }} da on da.icao = v.dest_icao
)
select flight_id, source, source_rank, origin_icao, dest_icao
from attached
where source_rn = 1
union all
select flight_id, source, source_rank, origin_icao, dest_icao
from vrs_votes
where origin_icao is not null or dest_icao is not null
