-- Finding 3, restated: the coherence gate must never fabricate a pair NOR promote a minority candidate
-- over a real plurality winner -- every served pair is attested AND each endpoint is top-vote or NULL.
with resolved as (
    select flight_id, origin_icao, dest_icao, origin_votes, dest_votes
    from {{ ref('fct_flights_reconciled') }}
    where origin_icao is not null and dest_icao is not null
      and origin_source != 'curated' and dest_source != 'curated'
),
attested as (
    select distinct flight_id, origin_icao, dest_icao
    from {{ ref('int_flight_attach') }}
    where origin_icao is not null and dest_icao is not null
)
select r.flight_id, r.origin_icao, r.dest_icao
from resolved r
left join attested a on a.flight_id = r.flight_id and a.origin_icao = r.origin_icao and a.dest_icao = r.dest_icao
where a.flight_id is null
   or r.origin_votes[r.origin_icao] < arrayMax(mapValues(r.origin_votes))
   or r.dest_votes[r.dest_icao] < arrayMax(mapValues(r.dest_votes))
