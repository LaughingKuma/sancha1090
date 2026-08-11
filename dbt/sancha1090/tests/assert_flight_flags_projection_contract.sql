-- Contract pin for the four projection classes: re-derive from reconciled, fail on any row present on one
-- side only. Diversion/military are aggregates — re-deriving would copy the model; class-nonempty guards them.
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
),
mart as (
    select flight_id, flag_class
    from {{ ref('fct_flight_flags') }}
    where flag_class in ('tiebreak_endpoint', 'single_source', 'one_sided_intl', 'feasibility_snap')
)
select
    coalesce(e.flight_id, m.flight_id)   as flight_id,
    coalesce(e.flag_class, m.flag_class) as flag_class,
    m.flight_id is null                  as missing_from_mart,
    e.flight_id is null                  as unexpected_in_mart
from expected e
full outer join mart m on m.flight_id = e.flight_id and m.flag_class = e.flag_class
where e.flight_id is null or m.flight_id is null
