-- Must match fct_flight_legs's snap endpoints on legs the callsign-split left untouched (same start+end).
-- A split-shortened leg keeps fct's start_time but gets an earlier end_time/dest by design -> join on both. SP2 removes the copy.
select o.icao24, o.start_time, o.origin_icao as opinion_origin, f.origin_icao as fct_origin,
       o.dest_icao as opinion_dest, f.dest_icao as fct_dest
from {{ ref('int_flight_legs_opensky') }} o
join {{ ref('fct_flight_legs') }} f
  on f.icao24 = o.icao24 and f.start_time = o.start_time and f.end_time = o.end_time
where (f.origin_source = 'snap' and o.origin_icao != f.origin_icao)
   or (f.dest_source   = 'snap' and o.dest_icao   != f.dest_icao)
