{{ config(materialized='table', tags=['adsb']) }}

-- Rooftop feed by registration country; the deliberate mirror of the global states agg_country_traffic.
select
    reg_country,
    count(distinct hex) as distinct_aircraft,
    count(*)            as observations,
    sum(case when is_military then 1 else 0 end) as military_observations
from {{ ref('fct_adsb_state') }}
where reg_country is not null
group by reg_country
order by distinct_aircraft desc
