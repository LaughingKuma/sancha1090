{{ config(materialized='table', tags=['reconcile']) }}

-- Daily movements at JP airports from the RECONCILED mart's consensus endpoints (SP2),
-- replacing raw bronze.opensky_flights — fixes RJCJ/RJCC and RJEC/RJCA mislabels.
with movements as (
    select
        origin_icao as airport_icao,
        'departure' as direction,
        icao24,
        -- JST day: departures count on the day they left (start_time).
        toDate(toTimeZone(start_time, 'Asia/Tokyo')) as traffic_day
    from {{ ref('fct_flights_reconciled') }}
    where origin_icao is not null and start_time is not null
    union all
    select
        dest_icao as airport_icao,
        'arrival' as direction,
        icao24,
        -- JST day: arrivals count on the day they landed (end_time).
        toDate(toTimeZone(end_time, 'Asia/Tokyo')) as traffic_day
    from {{ ref('fct_flights_reconciled') }}
    where dest_icao is not null and end_time is not null
)
select
    airport_icao,
    direction,
    traffic_day,
    count(*)               as flights,
    count(distinct icao24) as unique_aircraft
from movements
-- JP-only (RJ mainland + RO Ryukyu): foreign endpoints would show only their
-- Japan-touching flights, a misleading partial count.
where startsWith(airport_icao, 'RJ') or startsWith(airport_icao, 'RO')
group by 1, 2, 3
