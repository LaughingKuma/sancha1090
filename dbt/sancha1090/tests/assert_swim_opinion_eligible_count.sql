{{ config(tags=['swim']) }}
-- int_swim_opinion must equal exactly the vote-eligible int_swim_flight rows (incl. Finding-2 box gate).
select o.n as opinion_rows, e.n as eligible_rows
from (select count(*) n from {{ ref('int_swim_opinion') }}) o,
     (select count(*) n from {{ ref('int_swim_flight') }} f
       left join {{ ref('dim_airports') }} oa on oa.icao = f.origin_icao
       left join {{ ref('dim_airports') }} da on da.icao = f.dest_icao
       where f.icao24 is not null and f.hex_ambiguous = 0
         and (f.origin_icao is not null or f.dest_icao is not null)
         and (coalesce({{ in_japan_box('oa.lat', 'oa.lon') }}, false)
              or coalesce({{ in_japan_box('da.lat', 'da.lon') }}, false))) e
where o.n <> e.n
