-- Rank<->source bijection lock: fct_flights_reconciled's transform() decodes best_rank into the
-- provenance label, so a drifted stamp would silently mislabel origin_source/dest_source. Reads
-- int_flight_attach (opinions ∪ vrs_routes votes) so the attach-only voter is pinned too.
select source, source_rank
from {{ ref('int_flight_attach') }}
group by source, source_rank
having (source, source_rank) not in
       (('swim', 1), ('vrs_routes', 2), ('opensky_flights', 3), ('adsblol', 4), ('opensky_states', 5))
