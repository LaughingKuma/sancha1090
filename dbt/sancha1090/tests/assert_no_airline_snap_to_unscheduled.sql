-- The sched gate's invariant: an airline-shaped callsign can never have a SNAPPED endpoint
-- at an unscheduled airport (chained/curated endpoints are exempt — they're evidence, not snap).
select l.icao24, l.leg_id, l.callsign, l.origin_icao, l.dest_icao
from {{ ref('fct_flight_legs') }} l
left join {{ ref('dim_airports') }} oa on oa.icao = l.origin_icao
left join {{ ref('dim_airports') }} da on da.icao = l.dest_icao
where l.callsign is not null and match(trimBoth(l.callsign), '^[A-Z]{3}[0-9]')
  and ((l.origin_source = 'snap' and oa.icao is not null and not oa.scheduled_service)
    or (l.dest_source   = 'snap' and da.icao is not null and not da.scheduled_service))
