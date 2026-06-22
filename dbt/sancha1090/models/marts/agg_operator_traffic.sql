{{ config(materialized='table', tags=['flights']) }}

-- Flights per operator: registry operator wins; airline-coded callsigns (AAA123) fill
-- the gaps via dim_airlines, mirroring the guard used by the livemap MV.
with named as (
    select
        f.*,
        {% if target.type == 'clickhouse' %}
        trimBoth(coalesce(
            nullif(f.operator, ''),
            case when match(f.callsign, '^[A-Z]{3}[0-9]')
                 then al.name end
        )) as operator_name
        {% else %}
        trim(coalesce(
            nullif(f.operator, ''),
            case when regexp_like(f.callsign, '^[A-Z]{3}[0-9]')
                 then al.name end
        )) as operator_name
        {% endif %}
    from {{ ref('fact_flights') }} f
    left join {{ ref('dim_airlines') }} al
        on al.icao = {% if target.type == 'clickhouse' %}substring(f.callsign, 1, 3){% else %}substr(f.callsign, 1, 3){% endif %}
)
select
    operator_name,
    count(*)               as flight_count,
    count(distinct icao24) as distinct_aircraft,
    max(first_seen)        as last_flight
from named
where operator_name is not null
  and seen_in_context
group by 1
order by flight_count desc
