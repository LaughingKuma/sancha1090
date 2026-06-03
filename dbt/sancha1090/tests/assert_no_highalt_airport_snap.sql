-- Untagged: runs with fct_flight_legs under transform_marts (--exclude tag:adsb).
-- Fails if an endpoint snapped an airport to a cruise-altitude fix (an overflight, not a takeoff/landing).
select icao24, leg_id, first_alt_m, last_alt_m
from {{ ref('fct_flight_legs') }}
where (origin_icao is not null and first_alt_m >= {{ var('legs_cruise_alt_m') }})
   or (dest_icao   is not null and last_alt_m  >= {{ var('legs_cruise_alt_m') }})
