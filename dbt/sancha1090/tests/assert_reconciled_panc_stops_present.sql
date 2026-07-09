-- D2 canary: Anchorage tech stops are REAL (verified 2026-07-09); a "fix" that deletes them must fail loudly.
select 'panc_stops_missing' as problem
from (
    select countIf(origin_icao = 'PANC') as o, countIf(dest_icao = 'PANC') as d
    from {{ ref('fct_flights_reconciled') }}
)
where o = 0 or d = 0
