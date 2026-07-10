{{ config(tags=['reconcile']) }}
-- Finding 2: a schedule O/D with no endpoint in the observation box is observable only as a
-- transit -- its vote is unfalsifiable, so no such row may survive staging.
select v.callsign_norm
from {{ ref('stg_vrs_routes') }} v
join {{ ref('dim_airports') }} oa on oa.icao = v.origin_icao
join {{ ref('dim_airports') }} da on da.icao = v.dest_icao
where not {{ in_japan_box('oa.lat', 'oa.lon') }}
  and not {{ in_japan_box('da.lat', 'da.lon') }}
