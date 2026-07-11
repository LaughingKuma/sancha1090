{{ config(tags=['swim']) }}
-- int_swim_flight normalizes 3-letter LIDs with an iata match; none should survive into int_swim_opinion.
with lu as (select iata from {{ ref('dim_airports') }} where iata != '' group by iata)
select s.icao24
from {{ ref('int_swim_opinion') }} s
where (length(s.origin_icao) = 3 and s.origin_icao in (select iata from lu))
   or (length(s.dest_icao) = 3 and s.dest_icao in (select iata from lu))
