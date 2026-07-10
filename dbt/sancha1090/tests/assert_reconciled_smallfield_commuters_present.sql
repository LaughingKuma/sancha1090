-- SP4 canary: genuine non-jet small-field traffic (commuters, helos) must survive the feasibility
-- gate -- a future widening past jets must fail loudly (same discipline as the PANC canary).
-- warn not error: a fresh warehouse legitimately has none of these yet.
{{ config(severity='warn') }}

select 'smallfield_commuters_missing' as problem
from (
    select count() as n
    from {{ ref('fct_flights_reconciled') }} r
    left join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(r.icao24)
    where j.icao24 is null
      and (r.origin_icao in (select icao from {{ ref('dim_airports') }} where airport_type = 'small_airport')
           or r.dest_icao in (select icao from {{ ref('dim_airports') }} where airport_type = 'small_airport'))
)
where n = 0
