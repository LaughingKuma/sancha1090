-- #215: the seam of an over-cap, too-short-end-to-end run must not sit inside a chain born in that run. Runs and
-- seams are recomputed from staging with the chainer's own arms so a chainer regression cannot silently re-fuse.
with b as (
    select icao24, seg_start_time, seg_end_time, first_lat, first_lon, first_alt_m, first_on_ground, last_lat, last_lon,
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
    where icao24 is not null and seg_start_time is not null
),
f as (
    select *,
        dateDiff('second', prev_end_time, seg_start_time) as gap_s,
        {{ haversine_km('prev_last_lat', 'prev_last_lon', 'first_lat', 'first_lon') }}
            / (dateDiff('second', prev_end_time, seg_start_time) / 3600.0) as gap_kmh,
        case
            when prev_end_time is null then 1
            when dateDiff('second', prev_end_time, seg_start_time) <= 0 then 1
            when coalesce(prev_last_on_ground, true) or first_on_ground then 1
            when dateDiff('second', prev_end_time, seg_start_time) >= {{ var('chain_stop_gap_h') }} * 3600
                 and greatest(coalesce(prev_last_alt_m, 0), coalesce(first_alt_m, 0)) >= {{ var('chain_stop_alt_m') }}
                 and {{ haversine_km('prev_last_lat', 'prev_last_lon', 'first_lat', 'first_lon') }}
                         / (dateDiff('second', prev_end_time, seg_start_time) / 3600.0)
                     < {{ var('chain_stop_speed_kmh') }} then 1
            when dateDiff('second', prev_end_time, seg_start_time) >= {{ var('chain_low_fix_gap_min') }} * 60
                 and least(coalesce(prev_last_alt_m, 99999), coalesce(first_alt_m, 99999)) < {{ var('chain_low_fix_alt_m') }} then 1
            when {{ haversine_km('prev_last_lat', 'prev_last_lon', 'first_lat', 'first_lon') }}
                     / (dateDiff('second', prev_end_time, seg_start_time) / 3600.0)
                   not between {{ var('chain_speed_min_kmh') }} and {{ var('chain_speed_max_kmh') }} then 1
            else 0
        end as chain_break
    from b
),
runs as (
    select *,
        sum(chain_break) over (partition by icao24 order by seg_start_time, seg_end_time
                               rows between unbounded preceding and current row) as run_seq
    from f
),
shaped as (
    select *,
        min(seg_start_time) over w as run_start,
        (toUnixTimestamp(max(seg_end_time) over w) - toUnixTimestamp(min(seg_start_time) over w)) / 3600.0 as run_span_h,
        {{ haversine_km('tupleElement(argMin(tuple(first_lat, first_lon), seg_start_time) over w, 1)',
                        'tupleElement(argMin(tuple(first_lat, first_lon), seg_start_time) over w, 2)',
                        'tupleElement(argMax(tuple(last_lat, last_lon), seg_end_time) over w, 1)',
                        'tupleElement(argMax(tuple(last_lat, last_lon), seg_end_time) over w, 2)') }} as run_e2e_km,
        row_number() over (partition by icao24, run_seq
                           order by if(chain_break = 0 and gap_s >= {{ var('chain_low_fix_gap_min') }} * 60, 0, 1),
                                    ifNull(gap_kmh, inf), seg_start_time, seg_end_time) as seam_rank
    from runs
    window w as (partition by icao24, run_seq)
),
seams as (
    select icao24, seg_start_time, run_start
    from shaped
    where seam_rank = 1 and chain_break = 0 and gap_s >= {{ var('chain_low_fix_gap_min') }} * 60
      and run_span_h > {{ var('reconcile_anchor_max_hours') }}
      and run_e2e_km < {{ var('fused_envelope_speed_kmh') }} * (run_span_h - {{ var('fused_envelope_slack_h') }})
)
-- overlapping segments open a new run (the <= 0 arm), so an earlier run's chain can span this seam's time legitimately
select s.icao24, s.seg_start_time
from seams s
join {{ ref('int_flight_chains_adsblol') }} c
  on c.icao24 = s.icao24
 and c.chain_start >= s.run_start
 and s.seg_start_time > c.chain_start
 and s.seg_start_time <= c.chain_end
