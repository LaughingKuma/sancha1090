{{ config(materialized='table', tags=['flights']) }}

-- Daily arrivals/departures per tracked airport, over ALL of bronze.opensky_flights —
-- this is the multi-year trend line the v5.2 REST backfill extends backwards.
-- No fact_flights seen-join: pre-pipeline flights can't match antenna-era aircraft.
-- A flight surfaces in multiple capture windows (d0/d2/backfill): dedupe first.
with deduped as (
    select
        *,
        row_number() over (
            partition by icao24, first_seen, captured_for_airport, direction
            order by committed_at desc
        ) as rn
    from {{ source('bronze', 'opensky_flights') }}
)
select
    captured_for_airport as airport_icao,
    direction,
    -- Arrivals belong to the day they landed, departures to the day they left —
    -- in JST: these are Japanese airports, and UTC days would shift every
    -- 00:00-09:00 JST movement onto the previous calendar day.
    -- toTimeZone re-attaches JST to the UTC DateTime64 so toDate reads the JST calendar date.
    toDate(toTimeZone(
        case when direction = 'arrival' then last_seen else first_seen end,
        'Asia/Tokyo'
    )) as traffic_day,
    count(*) as flights,
    count(distinct icao24) as unique_aircraft
from deduped
where rn = 1
group by 1, 2, 3
