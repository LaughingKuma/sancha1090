-- #213C Finding 5: a one-fix leg carries no O/D vote -- one position snaps the same field at both ends and
-- would fabricate a same-airport flight. The leg row itself must survive (its anchor is VRS/swim's key).
select icao24, leg_id, num_fixes, origin_icao, dest_icao
from {{ ref('int_flight_legs_opensky') }}
where num_fixes < 2 and (origin_icao is not null or dest_icao is not null)
