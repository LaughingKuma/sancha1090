-- SP4: no reconciled AIRLINE-SHAPED jet endpoint may be jet-infeasible (short or unknown-runway
-- small field). Non-airline-shaped jets (bizjets, check flights) legitimately use short strips.
with ep as (
    select flight_id, icao24, callsign, origin_icao as airport, origin_source as src, 'origin' as side
    from {{ ref('fct_flights_reconciled') }} where origin_icao is not null
    union all
    select flight_id, icao24, callsign, dest_icao as airport, dest_source as src, 'dest' as side
    from {{ ref('fct_flights_reconciled') }} where dest_icao is not null
)
select ep.flight_id, ep.airport, ep.side
from ep
join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(ep.icao24)
join {{ ref('dim_airports') }} a on a.icao = ep.airport
where ep.src != 'curated'
  and {{ airline_shaped('ep.callsign') }}
  and {{ jet_infeasible_airport('a.runway_length_ft', 'a.airport_type') }}
