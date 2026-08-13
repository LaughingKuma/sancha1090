-- Deliberate SQL-shape pin: re-derives div_modal as a lock-step copy of the model's CTE chain.
-- The detail test's arm 2 proves rendered⇒gates (soundness); this proves eligible⇒rendered (completeness).
with recon as (
    select
        flight_id,
        trimBoth(callsign) as callsign,
        toDate(start_time) as start_day,
        origin_icao,
        dest_icao
    from {{ ref('fct_flights_reconciled') }}
    where flight_id is not null
),
-- Candidate pool matches the model's div_base exactly: full airline-shaped both-resolved population,
-- NOT narrowed to same-endpoint flights — narrowing would starve the window support this check reads.
div_base as (
    select
        r.flight_id       as flight_id,
        upper(r.callsign) as cs,
        r.origin_icao     as org,
        r.start_day       as d,
        r.dest_icao       as dest_icao
    from recon r
    -- airline_shaped() inlined on purpose: the model calls the macro, so a macro regression would move
    -- model and oracle together — the oracle pins the predicate's current expansion instead.
    where (r.callsign is not null and match(trimBoth(r.callsign), '^[A-Z]{3}[0-9]'))
      and r.origin_icao is not null
      and r.dest_icao is not null
),
div_daily as (
    select cs, org, d, dest_icao, count() as n
    from div_base
    group by cs, org, d, dest_icao
),
div_win as (
    select b.flight_id as flight_id, b.dest_icao as dest_icao, c.dest_icao as cand, sum(c.n) as n
    from div_base b
    join div_daily c on c.cs = b.cs and c.org = b.org
    where c.d <= b.d and c.d > b.d - {{ var('flag_diversion_window_days') }}
    group by b.flight_id, b.dest_icao, c.dest_icao
),
-- tuple(n, cand) total order pinned exactly as the model: an untied argMax drifts between runs.
div_modal as (
    select
        flight_id,
        dest_icao,
        sum(n)                        as support_total,
        argMax(cand, tuple(n, cand))  as modal_dest,
        max(n)                        as modal_n
    from div_win
    group by flight_id, dest_icao
),

-- ELIGIBLE: same_endpoint members (both endpoints resolved and equal) whose recomputed modal clears
-- every render gate the model checks — the independent oracle for what "should" render modal-bearing.
eligible as (
    select
        r.flight_id     as flight_id,
        r.origin_icao   as origin_icao,
        m.modal_dest    as modal_dest,
        m.modal_n       as modal_n,
        m.support_total as support_total
    from recon r
    join div_modal m on m.flight_id = r.flight_id
    where r.origin_icao is not null
      and r.dest_icao is not null
      and r.origin_icao = r.dest_icao
      and m.modal_dest != r.origin_icao
      and m.modal_n >= {{ var('flag_diversion_min_support') }}
      and m.modal_n / m.support_total >= {{ var('flag_diversion_min_share') }}
),
same_endpoint as (
    select f.flight_id, f.detail
    from {{ ref('fct_flight_flags') }} f
    where f.flag_class = 'same_endpoint'
),

-- A) ELIGIBLE -> RENDERED: every eligible flight's mart detail must equal the modal string exactly.
-- A missing row (left join NULL), a bare form, or mismatched figures are all violations here.
eligible_modal_not_rendered as (
    select e.flight_id, 'eligible_modal_not_rendered' as violation
    from eligible e
    left join same_endpoint s on s.flight_id = e.flight_id
    where coalesce(s.detail, '') != concat('at ', e.origin_icao, ' vs modal ', e.modal_dest, ' ',
                                            toString(e.modal_n), '/', toString(e.support_total))
),

-- B) RENDERED -> ELIGIBLE: the converse leak — a modal-bearing detail whose flight the oracle does
-- not consider eligible. Presence-marker LEFT JOIN, not bare IS NULL (round-2 join_use_nulls lesson).
rendered_without_eligibility as (
    select mb.flight_id, 'rendered_without_eligibility' as violation
    from (
        select flight_id
        from same_endpoint
        where match(detail, '^at [A-Z0-9]{4} vs modal [A-Z0-9]{4} [0-9]+/[0-9]+$')
    ) mb
    left join (select flight_id, 1 as present from eligible) e on e.flight_id = mb.flight_id
    where coalesce(e.present, 0) = 0
)

select flight_id, violation from eligible_modal_not_rendered
union all
select flight_id, violation from rendered_without_eligibility
