{{ config(
    materialized='table',
    tags=['reconcile'],
    query_settings={'max_memory_usage': 4000000000},
) }}

-- SP4 + Finding 3: vrs_routes votes but never anchors (standing data has no window). Multi-hop schedules
-- explode into box-gated legs; a lone box-leg votes unconditionally, a multi-leg schedule votes only the
-- leg its own observed endpoints uniquely corroborate (else abstains). One vote/flight; jet-gated as usual.
with vrs_cand as (
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
    left join {{ ref('int_flight_attached_votes') }} a
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
select flight_id, source, source_rank, origin_icao, dest_icao, origin_gated, dest_gated
from {{ ref('int_flight_attached_votes') }}
union all
-- The vrs gate is deliberately unrepresented: admitting its both-NULL rows to carry a flag would raise
-- uniqExact(source) and silently move fct_flights_reconciled.n_sources, a published column.
select flight_id, source, source_rank, origin_icao, dest_icao,
       toUInt8(0) as origin_gated, toUInt8(0) as dest_gated
from vrs_votes
where origin_icao is not null or dest_icao is not null
