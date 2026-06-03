-- Untagged: runs with fct_flight_legs under transform_marts (--exclude tag:adsb).
-- Fails if a leg key fans out: each (icao24, leg_id) must be exactly one row.
select icao24, leg_id, count(*) as n
from {{ ref('fct_flight_legs') }}
group by icao24, leg_id
having count(*) > 1
