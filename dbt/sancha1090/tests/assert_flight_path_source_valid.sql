-- Source enum, position present, and altitude (when present) within a coarse sanity band. The mart carries
-- source altitudes verbatim, so rare decode garbage reaches ~124k ft; the 130k ceiling only rejects absurd
-- values -- it is NOT a conversion guard (unit correctness is pinned by assert_flight_path_unit_conversion).
select flight_id, ts, source, lat, lon, alt_ft
from {{ ref('fct_flight_path') }}
where source not in ('adsb', 'adsblol', 'opensky')
   or lat is null or lon is null
   or (alt_ft is not null and (alt_ft < -1500 or alt_ft > 130000))
