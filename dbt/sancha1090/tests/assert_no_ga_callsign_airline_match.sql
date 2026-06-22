{{ config(tags=['adsb']) }}
-- Fails if a GA/registration callsign (e.g. JA45KA) false-matched an airline despite the regex guard.
-- Checks callsign_filled (the OpenSky-backfilled callsign that now drives the airline join), not raw flight.
select hex, callsign_filled, airline_name
from {{ ref('fct_adsb_state') }}
where airline_name is not null
  and not match(trimBoth(callsign_filled), '^[A-Z]{3}[0-9]')
