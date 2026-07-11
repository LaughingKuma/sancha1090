{{ config(tags=['reconcile']) }}
-- Finding 3: a multi-leg vrs vote (n_box_legs >= 2) requires exactly one position-aligned,
-- observation-corroborated candidate. Every non-NULL endpoint emitted by the vote must belong to that
-- sole supported leg; jet gating may NULL either endpoint, but int_flight_attach drops both-NULL votes.
with vrs_votes as (
    select flight_id, origin_icao as voted_origin, dest_icao as voted_dest
    from {{ ref('int_flight_attach') }}
    where source = 'vrs_routes'
),
flight_norm as (
    select sp.flight_id as flight_id, {{ callsign_norm('sp.anchor_callsign') }} as callsign_norm
    from {{ ref('int_flight_spine') }} sp
),
observed as (
    select flight_id, origin_icao, dest_icao
    from {{ ref('int_flight_attach') }}
    where source != 'vrs_routes'
),
legs as (
    select vv.flight_id as flight_id, vv.voted_origin as voted_origin, vv.voted_dest as voted_dest,
           v.n_box_legs as n_box_legs,
           v.origin_icao as leg_origin, v.dest_icao as leg_dest
    from vrs_votes vv
    join flight_norm fn on fn.flight_id = vv.flight_id
    join {{ ref('stg_vrs_routes') }} v on v.callsign_norm = fn.callsign_norm
),
corroborated as (
    select l.flight_id as flight_id, l.voted_origin as voted_origin, l.voted_dest as voted_dest,
           l.n_box_legs as n_box_legs, l.leg_origin as leg_origin, l.leg_dest as leg_dest,
           countIf(o.origin_icao = l.leg_origin or o.dest_icao = l.leg_dest) as support_cnt
    from legs l
    left join observed o on o.flight_id = l.flight_id
    group by l.flight_id, l.voted_origin, l.voted_dest, l.n_box_legs, l.leg_origin, l.leg_dest
)
select flight_id
from corroborated
where n_box_legs >= 2
group by flight_id, voted_origin, voted_dest
having countIf(support_cnt > 0) != 1
    or countIf(
        support_cnt > 0
        and (voted_origin is null or voted_origin = leg_origin)
        and (voted_dest is null or voted_dest = leg_dest)
    ) != 1
