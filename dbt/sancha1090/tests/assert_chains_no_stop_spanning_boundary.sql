-- D1: no intra-chain boundary may violate the stop-spanning break arms (slow long-gap or low-fix+gap);
-- recomputed from staging so a chainer regression can't silently re-fuse. Speed bounds are intentional,
-- not drift: they scope to boundaries that would have CHAINED under the speed-band rule, so an
-- overlapping-chain-window edge case can't false-positive as intra-chain.
with b as (
    select icao24, seg_start_time, first_lat, first_lon, first_alt_m, first_on_ground,
        lagInFrame(toNullable(seg_end_time), 1, NULL)
            over (partition by icao24 order by seg_start_time, seg_end_time rows between 1 preceding and current row) as prev_end_time,
        lagInFrame(toNullable(last_lat), 1, NULL)
            over (partition by icao24 order by seg_start_time, seg_end_time rows between 1 preceding and current row) as prev_last_lat,
        lagInFrame(toNullable(last_lon), 1, NULL)
            over (partition by icao24 order by seg_start_time, seg_end_time rows between 1 preceding and current row) as prev_last_lon,
        lagInFrame(toNullable(last_alt_m), 1, NULL)
            over (partition by icao24 order by seg_start_time, seg_end_time rows between 1 preceding and current row) as prev_last_alt_m,
        lagInFrame(toNullable(last_on_ground), 1, NULL)
            over (partition by icao24 order by seg_start_time, seg_end_time rows between 1 preceding and current row) as prev_last_on_ground
    from {{ ref('stg_flight_segments_adsblol') }}
),
violations as (
    select icao24, seg_start_time
    from b
    where prev_end_time is not null
      and dateDiff('second', prev_end_time, seg_start_time) > 0
      and not (coalesce(prev_last_on_ground, true) or first_on_ground)
      and (
        (dateDiff('second', prev_end_time, seg_start_time) >= {{ var('chain_stop_gap_h') }} * 3600
         and greatest(coalesce(prev_last_alt_m, 0), coalesce(first_alt_m, 0)) >= {{ var('chain_stop_alt_m') }}
         and {{ haversine_km('prev_last_lat', 'prev_last_lon', 'first_lat', 'first_lon') }}
                 / (dateDiff('second', prev_end_time, seg_start_time) / 3600.0)
             between {{ var('chain_speed_min_kmh') }} and {{ var('chain_stop_speed_kmh') }})
        or
        (dateDiff('second', prev_end_time, seg_start_time) >= {{ var('chain_low_fix_gap_min') }} * 60
         and least(coalesce(prev_last_alt_m, 99999), coalesce(first_alt_m, 99999)) < {{ var('chain_low_fix_alt_m') }}
         and {{ haversine_km('prev_last_lat', 'prev_last_lon', 'first_lat', 'first_lon') }}
                 / (dateDiff('second', prev_end_time, seg_start_time) / 3600.0)
             between {{ var('chain_speed_min_kmh') }} and {{ var('chain_speed_max_kmh') }})
      )
)
select v.icao24, v.seg_start_time
from violations v
join {{ ref('int_flight_chains_adsblol') }} c
  on c.icao24 = v.icao24
 and v.seg_start_time > c.chain_start
 and v.seg_start_time <= c.chain_end
