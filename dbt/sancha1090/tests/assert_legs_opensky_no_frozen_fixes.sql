-- #213C Finding 4: every airborne fix that survives the frozen-run collapse (recomputed from the snapshots with
-- the model's own arm) must sit inside a leg window, and each leg's num_fixes must equal the fixes inside it.
with fixes as (
    select icao24, snapshot_time, on_ground,
        sum(if(prev_time is null or dateDiff('second', prev_time, snapshot_time) > {{ var('legs_gap_min') }} * 60
               or not (latitude <=> prev_lat) or not (longitude <=> prev_lon) or not (on_ground <=> prev_ground), 1, 0))
            over (partition by icao24 order by snapshot_time rows between unbounded preceding and current row) as streak_id
    from (
        select icao24, snapshot_time, latitude, longitude, on_ground,
            lagInFrame(toNullable(snapshot_time), 1, NULL)
                over (partition by icao24 order by snapshot_time rows between 1 preceding and current row) as prev_time,
            lagInFrame(toNullable(latitude), 1, NULL)
                over (partition by icao24 order by snapshot_time rows between 1 preceding and current row) as prev_lat,
            lagInFrame(toNullable(longitude), 1, NULL)
                over (partition by icao24 order by snapshot_time rows between 1 preceding and current row) as prev_lon,
            lagInFrame(toNullable(on_ground), 1, NULL)
                over (partition by icao24 order by snapshot_time rows between 1 preceding and current row) as prev_ground
        from {{ ref('fact_state_snapshots') }}
    ) lagged
),
kept as (
    select icao24, snapshot_time
    from (
        select *,
            min(snapshot_time) over (partition by icao24, streak_id) as streak_start,
            max(snapshot_time) over (partition by icao24, streak_id) as streak_end
        from fixes
        where not on_ground
    ) bounded
    where snapshot_time = streak_start
       or dateDiff('second', streak_start, streak_end) <= {{ var('legs_frozen_min') }} * 60
),
-- legs are contiguous in time per airframe, so the latest leg starting at or before a fix is its only candidate
placed as (
    select k.icao24 as icao24, k.snapshot_time as snapshot_time, l.leg_id as leg_id,
           coalesce(k.snapshot_time <= l.end_time, 0) as in_window
    from kept k
    asof left join {{ ref('int_flight_legs_opensky') }} l on l.icao24 = k.icao24 and k.snapshot_time >= l.start_time
),
-- one pass over placed: the snapshot chain above it is the whole cost, and a second reference would run it twice
counted as (
    select icao24, leg_id, countIf(in_window) as n_kept, countIf(not in_window) as n_orphan,
           minIf(snapshot_time, not in_window) as first_orphan
    from placed
    group by icao24, leg_id
)
select coalesce(l.icao24, c.icao24) as icao24, coalesce(l.leg_id, c.leg_id) as leg_id,
       l.num_fixes, coalesce(c.n_kept, 0) as n_kept, c.first_orphan as snapshot_time,
       concat(if(l.num_fixes != coalesce(c.n_kept, 0),
                 'num_fixes differs from the kept airborne fixes inside the window; ', ''),
              if(c.n_orphan > 0, 'kept airborne fix inside no leg window', '')) as reason
from {{ ref('int_flight_legs_opensky') }} l
full outer join counted c on c.icao24 = l.icao24 and c.leg_id = l.leg_id
where l.num_fixes != coalesce(c.n_kept, 0) or c.n_orphan > 0
