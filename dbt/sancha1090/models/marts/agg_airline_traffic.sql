-- Pure OpenSky-context-feed mart: built by transform_marts (untagged) so it refreshes on OpenSky context ticks.
{{ config(materialized='table', tags=['ch_mv']) }}

-- Hourly distinct airframes by airline from the OpenSky context feed (callsign -> dim_airlines), not leg-derived.
select
    toStartOfHour(s.snapshot_time) as snapshot_hour,
    al.name    as airline_name,
    al.country as airline_country,
    count(distinct s.icao24) as distinct_aircraft,
    count(*)                 as observations
from {{ ref('fact_state_snapshots') }} s
-- Operating airline via callsign prefix; same GA-tail regex guard as silver to avoid false matches.
join {{ ref('dim_airlines') }} al
  on al.icao = substring(trimBoth(s.callsign), 1, 3)
 and match(trimBoth(s.callsign), '^[A-Z]{3}[0-9]')
group by 1, 2, 3
order by snapshot_hour, distinct_aircraft desc
