-- #213C: two byte-identical airborne fixes more than legs_gap_min apart are two sessions, not one frozen run:
-- the later fix must survive the collapse and open a leg after the earlier one, as it did before the collapse.
with pairs as (
    select icao24, snapshot_time, prev_time
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
    where not on_ground and not prev_ground
      and latitude <=> prev_lat and longitude <=> prev_lon
      and dateDiff('second', prev_time, snapshot_time) > {{ var('legs_gap_min') }} * 60
)
select p.icao24, p.prev_time, p.snapshot_time, l.leg_id, l.start_time, l.end_time
from pairs p
asof left join {{ ref('int_flight_legs_opensky') }} l on l.icao24 = p.icao24 and p.snapshot_time >= l.start_time
where not coalesce(p.snapshot_time <= l.end_time and l.start_time > p.prev_time, 0)
