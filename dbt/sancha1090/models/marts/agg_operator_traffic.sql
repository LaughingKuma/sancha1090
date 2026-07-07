{{ config(materialized='table', tags=['reconcile']) }}

-- Flights per operator on the reconciled mart: registry operator wins; airline_name (already resolved
-- from the callsign in the mart) fills the gap. tag:reconcile -> builds in transform_marts.
with named as (
    select
        r.icao24 as icao24,
        r.start_time as start_time,
        trimBoth(coalesce(nullif(ac.operator, ''), r.airline_name)) as operator_name
    from {{ ref('fct_flights_reconciled') }} r
    left join {{ ref('dim_aircraft') }} ac on ac.icao24 = lower(r.icao24)
)
select
    operator_name,
    count(*)               as flight_count,
    count(distinct icao24) as distinct_aircraft,
    max(start_time)        as last_flight
from named
where operator_name is not null and operator_name != ''
group by 1
order by flight_count desc
