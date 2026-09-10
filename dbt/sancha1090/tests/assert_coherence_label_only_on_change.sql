-- Finding 3: 'coherence' must mark only a value the gate actually changed -- never a rescue or fallback
-- side that happens to equal the ballot's own winner (that side keeps its original agreement instead).
with resolved as (
    select flight_id, origin_icao, origin_agreement, dest_icao, dest_agreement
    from {{ ref('fct_flights_reconciled') }}
    where coalesce(origin_source, '') != 'curated' and coalesce(dest_source, '') != 'curated'
      and (origin_agreement = 'coherence' or dest_agreement = 'coherence')
)
select r.flight_id, 'origin' as endpoint
from resolved r
join {{ ref('int_flight_ballot') }} b on b.flight_id = r.flight_id
where r.origin_agreement = 'coherence'
  and not (r.origin_icao is null and length(b.origin_votes) > 0)
  and not (r.origin_icao is not null and r.origin_icao != b.origin_icao)
union all
select r.flight_id, 'dest' as endpoint
from resolved r
join {{ ref('int_flight_ballot') }} b on b.flight_id = r.flight_id
where r.dest_agreement = 'coherence'
  and not (r.dest_icao is null and length(b.dest_votes) > 0)
  and not (r.dest_icao is not null and r.dest_icao != b.dest_icao)
