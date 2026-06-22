{{ config(materialized='table', tags=['adsb', 'ch_mv']) }}

-- Rooftop ADS-B feed by operating airline (callsign -> dim_airlines); the deliberate mirror of the
-- OpenSky context agg_airline_traffic, but scoped to what THIS antenna actually received. backfilled_obs
-- shows how many observations only attribute to an airline thanks to the OpenSky callsign backfill.
select
    airline_name,
    airline_country,
    count(distinct hex)                                           as distinct_aircraft,
    count(*)                                                      as observations,
    sum(case when callsign_source = 'opensky_backfill' then 1 else 0 end) as backfilled_observations
from {{ ref('fct_adsb_state') }}
where airline_name is not null
group by airline_name, airline_country
order by distinct_aircraft desc
