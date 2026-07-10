-- SP4 canary: non-airline-shaped jets (bizjets, check flights) at small fields are REAL (verified
-- 2026-07-09, JA10MZ C25C @ Kohnan class); the gate widening past airline-shaped must fail loudly.
-- warn not error: a fresh warehouse legitimately has none of these yet.
{{ config(severity='warn') }}

select 'smallfield_bizjets_missing' as problem
from (
    select count() as n
    from {{ ref('fct_flights_reconciled') }} r
    join {{ ref('int_jet_airframes') }} j on j.icao24 = lower(r.icao24)
    where not {{ airline_shaped('r.callsign') }}
      and (r.origin_icao in (select icao from {{ ref('dim_airports') }} where airport_type = 'small_airport')
           or r.dest_icao in (select icao from {{ ref('dim_airports') }} where airport_type = 'small_airport'))
)
where n = 0
