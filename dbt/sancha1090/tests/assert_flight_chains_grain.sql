-- One chain per (icao24, chain_start) and a sane window; fanout here would fan out fct_flight_legs.
select icao24, chain_start
from {{ ref('int_flight_chains_adsblol') }}
group by icao24, chain_start
having count(*) > 1 or max(chain_end) < max(chain_start)
