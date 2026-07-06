-- adsblol-lane mirror of assert_no_airline_snap_to_unscheduled: gated at the segment snap.
select r.icao24, r.seg_start_time, r.callsign, r.origin_icao, r.dest_icao
from {{ ref('int_flight_routes_adsblol') }} r
left join {{ ref('dim_airports') }} oa on oa.icao = r.origin_icao
left join {{ ref('dim_airports') }} da on da.icao = r.dest_icao
where r.callsign is not null and match(trimBoth(r.callsign), '^[A-Z]{3}[0-9]')
  and ((oa.icao is not null and not oa.scheduled_service)
    or (da.icao is not null and not da.scheduled_service))
