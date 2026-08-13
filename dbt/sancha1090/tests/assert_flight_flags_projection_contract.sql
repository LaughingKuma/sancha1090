-- Contract pin for the five projection classes: re-derive from reconciled, fail on any one-sided row (presence markers, not NULL joins, keep it join_use_nulls-independent).
-- Diversion/military are aggregates guarded by class-nonempty; same_endpoint's detail reads the diversion modal, its membership does not.
with expected as (
    select r.flight_id as flight_id, 'tiebreak_endpoint' as flag_class
    from {{ ref('fct_flights_reconciled') }} r
    where r.flight_id is not null
      and (r.origin_agreement = 'tiebreak' or r.dest_agreement = 'tiebreak')

    union all

    select r.flight_id, 'single_source'
    from {{ ref('fct_flights_reconciled') }} r
    where r.flight_id is not null and r.n_sources = 1

    union all

    select r.flight_id, 'one_sided_intl'
    from {{ ref('fct_flights_reconciled') }} r
    where r.flight_id is not null and (r.origin_icao is null) != (r.dest_icao is null)

    union all

    select r.flight_id, 'feasibility_snap'
    from {{ ref('fct_flights_reconciled') }} r
    where r.flight_id is not null and r.feasibility_gated = 1

    union all

    select r.flight_id, 'same_endpoint'
    from {{ ref('fct_flights_reconciled') }} r
    where r.flight_id is not null
      and r.origin_icao is not null
      and r.dest_icao is not null
      and r.origin_icao = r.dest_icao
),
mart as (
    select flight_id, flag_class
    from {{ ref('fct_flight_flags') }}
    where flag_class in ('tiebreak_endpoint', 'single_source', 'one_sided_intl',
                         'feasibility_snap', 'same_endpoint')
)
select
    if(coalesce(e.present, 0) = 1, e.flight_id, m.flight_id)   as flight_id,
    if(coalesce(e.present, 0) = 1, e.flag_class, m.flag_class) as flag_class,
    coalesce(m.present, 0) = 0                                 as missing_from_mart,
    coalesce(e.present, 0) = 0                                 as unexpected_in_mart
from (select flight_id, flag_class, 1 as present from expected) e
full outer join (select flight_id, flag_class, 1 as present from mart) m
    on m.flight_id = e.flight_id and m.flag_class = e.flag_class
where coalesce(e.present, 0) = 0 or coalesce(m.present, 0) = 0
