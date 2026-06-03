{{ config(tags=['adsb']) }}
-- Fails if a GA/registration callsign (e.g. JA45KA) false-matched an airline despite the regex guard.
select hex, flight, airline_name
from {{ ref('fct_adsb_state') }}
where airline_name is not null
  and not regexp_like(trim(flight), '^[A-Z]{3}[0-9]')
