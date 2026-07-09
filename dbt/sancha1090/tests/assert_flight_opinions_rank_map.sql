-- Rank<->source bijection lock: fct_flights_reconciled's transform() decodes best_rank into the
-- provenance label, so a drifted stamp would silently mislabel origin_source/dest_source.
select source, source_rank
from {{ ref('int_flight_opinions') }}
group by source, source_rank
having (source, source_rank) not in
       (('swim', 1), ('opensky_flights', 2), ('adsblol', 3), ('opensky_states', 4))
