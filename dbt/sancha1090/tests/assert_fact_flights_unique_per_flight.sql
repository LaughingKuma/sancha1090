{{ config(tags=['flights']) }}

-- The (icao24, first_seen) dedupe must collapse every multi-window/multi-list capture.
select icao24, first_seen, count(*) as n
from {{ ref('fact_flights') }}
group by 1, 2
having count(*) > 1
