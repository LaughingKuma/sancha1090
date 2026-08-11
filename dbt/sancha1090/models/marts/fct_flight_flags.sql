{{ config(materialized='table') }}

with recon as (
    select
        flight_id,
        lower(icao24)      as icao24,
        trimBoth(callsign) as callsign,
        toDate(start_time) as start_day,
        anchor_source,
        n_sources,
        origin_icao, origin_agreement,
        dest_icao, dest_agreement,
        feasibility_gated
    from {{ ref('fct_flights_reconciled') }}
    where flight_id is not null
),
-- Modal dest is keyed on (callsign, origin) and airline-shaped only: callsign alone reads every cargo
-- return leg as a diversion (CKS785 RKSI->PANC vs modal PANC) and fuses 245 '@@@@@@@@' flights into one service.
div_base as (
    select
        r.flight_id      as flight_id,
        upper(r.callsign) as cs,
        r.origin_icao    as org,
        r.start_day      as d,
        r.dest_icao      as dest_icao
    from recon r
    where {{ airline_shaped('r.callsign') }}
      and r.origin_icao is not null
      and r.dest_icao is not null
),
div_daily as (
    select cs, org, d, dest_icao, count() as n
    from div_base
    group by cs, org, d, dest_icao
),
div_win as (
    -- trailing window is inclusive of the flight's own day and of the flight itself: a one-off dest
    -- must still count toward its own support, else a service's first sighting always reads as modal-1.
    select b.flight_id as flight_id, b.dest_icao as dest_icao, c.dest_icao as cand, sum(c.n) as n
    from div_base b
    join div_daily c on c.cs = b.cs and c.org = b.org
    where c.d <= b.d and c.d > b.d - {{ var('flag_diversion_window_days') }}
    group by b.flight_id, b.dest_icao, c.dest_icao
),
div_modal as (
    -- tuple(n, cand) makes the argMax a total order; an untied argMax drifted the flag set between runs.
    select
        flight_id,
        dest_icao,
        sum(n)                        as support_total,
        argMax(cand, tuple(n, cand))  as modal_dest,
        max(n)                        as modal_n
    from div_win
    group by flight_id, dest_icao
)

-- concat() and UNION ALL both promote to Nullable from a single nullable ingredient, so every arm
-- coalesces its ingredients to keep `detail` a plain String for both dbt and the livemap client.
select
    r.flight_id                                                              as flight_id,
    r.icao24                                                                 as icao24,
    r.callsign                                                               as callsign,
    r.start_day                                                              as start_day,
    'tiebreak_endpoint'                                                      as flag_class,
    concat('origin ', coalesce(r.origin_agreement, '-'),
           ' / dest ', coalesce(r.dest_agreement, '-'))                      as detail
from recon r
where r.origin_agreement = 'tiebreak' or r.dest_agreement = 'tiebreak'

union all

select
    r.flight_id,
    r.icao24,
    r.callsign,
    r.start_day,
    'single_source',
    concat('only ', r.anchor_source)
from recon r
where r.n_sources = 1

union all

select
    r.flight_id,
    r.icao24,
    r.callsign,
    r.start_day,
    'one_sided_intl',
    if(r.origin_icao is null,
       concat('origin unresolved / dest ', coalesce(r.dest_icao, '-')),
       concat('origin ', coalesce(r.origin_icao, '-'), ' / dest unresolved'))
from recon r
where (r.origin_icao is null) != (r.dest_icao is null)

union all

select
    r.flight_id,
    r.icao24,
    r.callsign,
    r.start_day,
    'feasibility_snap',
    'SP4 jet runway feasibility gate'
from recon r
where r.feasibility_gated = 1

union all

select
    r.flight_id,
    r.icao24,
    r.callsign,
    r.start_day,
    'diversion',
    concat('dest ', coalesce(m.dest_icao, '-'), ' vs modal ', coalesce(m.modal_dest, '-'),
           ' ', toString(m.modal_n), '/', toString(m.support_total))
from div_modal m
join recon r on r.flight_id = m.flight_id
where m.modal_n >= {{ var('flag_diversion_min_support') }}
  and m.modal_n / m.support_total >= {{ var('flag_diversion_min_share') }}
  and m.dest_icao != m.modal_dest

union all

select
    r.flight_id,
    r.icao24,
    r.callsign,
    r.start_day,
    'military',
    ''
from recon r
join {{ ref('fct_flight_recon_tier') }} t on t.flight_id = r.flight_id
where t.is_military = 1
