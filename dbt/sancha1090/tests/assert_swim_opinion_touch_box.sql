{{ config(tags=['swim']) }}
-- Finding 2: a filed O/D with no in-box endpoint is a transit stamp; it must not be vote-eligible.
select s.icao24
from {{ ref('int_swim_opinion') }} s
left join {{ ref('dim_airports') }} oa on oa.icao = s.origin_icao
left join {{ ref('dim_airports') }} da on da.icao = s.dest_icao
where not coalesce({{ in_japan_box('oa.lat', 'oa.lon') }}, false)
  and not coalesce({{ in_japan_box('da.lat', 'da.lon') }}, false)
