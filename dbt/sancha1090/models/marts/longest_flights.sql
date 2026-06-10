{{ config(materialized='table', tags=['flights']) }}

-- Top-N longest flights per day among aircraft the context feed surfaced.
with ranked as (
    select
        date(first_seen) as flight_day,
        callsign,
        icao24,
        registration,
        operator,
        model,
        origin_icao,
        origin_city,
        dest_icao,
        dest_city,
        first_seen,
        flight_duration_seconds,
        row_number() over (
            partition by date(first_seen)
            order by flight_duration_seconds desc
        ) as day_rank
    from {{ ref('fact_flights') }}
    where flight_duration_seconds is not null
      and seen_in_context
)
select * from ranked
where day_rank <= 10
order by flight_day desc, day_rank
