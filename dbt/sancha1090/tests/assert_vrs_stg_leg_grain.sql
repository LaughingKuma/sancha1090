{{ config(tags=['reconcile']) }}
-- Finding 3: stg_vrs_routes grain is one row per (callsign_norm, origin_icao, dest_icao) box-gated
-- leg; a duplicate would double-weight one leg's schedule prior against observation.
select callsign_norm, origin_icao, dest_icao
from {{ ref('stg_vrs_routes') }}
group by callsign_norm, origin_icao, dest_icao
having count() > 1
