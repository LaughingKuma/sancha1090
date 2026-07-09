-- D1: fused windows from collapse-prone sources must neither anchor nor vote. Both conditions are
-- recomputed independently (the envelope from dim_airports coords, not the model's own filter), so a
-- dropped filter clause can't pass tautologically.
with o as (
    select source, icao24, win_start, win_end, origin_icao, dest_icao,
           (toUnixTimestamp(win_end) - toUnixTimestamp(win_start)) / 3600.0 as dur_h
    from {{ ref('int_flight_opinions') }}
    where source in ('opensky_flights', 'adsblol')
)
select o.source, o.icao24, o.win_start, round(o.dur_h, 1) as dur_h
from o
left join {{ ref('dim_airports') }} oa on oa.icao = o.origin_icao
left join {{ ref('dim_airports') }} da on da.icao = o.dest_icao
where o.dur_h > {{ var('reconcile_anchor_max_hours') }}
   or (o.source = 'opensky_flights'
       and oa.lat is not null and oa.lon is not null and da.lat is not null and da.lon is not null
       and o.dur_h >= 8
       and {{ haversine_km('oa.lat', 'oa.lon', 'da.lat', 'da.lon') }}
             < {{ var('fused_envelope_speed_kmh') }} * (o.dur_h - 1.5))
