{{ config(materialized='table', tags=['reconcile']) }}

-- Top-N longest reconciled flights per day: operator/model from dim_aircraft, cities from the mart's endpoint geo.
-- Plausibility filters drop fused adsb.lol chains (e.g. a 76h Vancouver->Vancouver self-loop); 24h ceiling keeps real ~19h ultra-long-hauls.
with ranked as (
    select
        toDate(r.start_time) as flight_day,
        r.callsign,
        r.icao24 as icao24,
        r.registration as registration,
        ac.operator,
        ac.model,
        r.origin_icao,
        r.origin_city,
        r.dest_icao,
        r.dest_city,
        r.start_time as first_seen,
        dateDiff('second', r.start_time, r.end_time) as flight_duration_seconds,
        row_number() over (
            partition by toDate(r.start_time)
            order by dateDiff('second', r.start_time, r.end_time) desc
        ) as day_rank
    from {{ ref('fct_flights_reconciled') }} r
    left join {{ ref('dim_aircraft') }} ac on ac.icao24 = lower(r.icao24)
    where r.start_time is not null and r.end_time is not null
      and r.origin_icao is not null and r.dest_icao is not null and r.origin_icao != r.dest_icao
      and dateDiff('second', r.start_time, r.end_time) between 1 and 86400
)
select * from ranked
where day_rank <= 10
order by flight_day desc, day_rank
