{{ config(materialized='table', tags=['swim']) }}

-- SWIM has no Mode-S hex: resolve icao24 by DENSITY of observed callsign-matched snapshots in the filed window,
-- suppressing ambiguous top-two to NULL; latest amendment per flight = argMax over the endpoint tuple (atomic).
with latest as (
    select
        coalesce(gufi, flight_ref, concat(assumeNotNull(acid), '|', ifNull(computer_id,''), '|',
                 toString(toDate(filed_departure_time)))) as flight_key,
        argMax(tuple(dep_point, dep_point_kind, arr_point, arr_point_kind,
                     filed_departure_time, filed_arrival_time, acid),
               tuple(msg_timestamp, _dedup_fp)) as latest_tuple  -- version = @sourceTimeStamp (spike-confirmed)
    from {{ source('bronze', 'swim_flightdata') }}
    where acid is not null and trimBoth(acid) <> ''
    group by flight_key
),
flat as (
    select flight_key,
        latest_tuple.1 as origin_icao, latest_tuple.2 as dep_point_kind,
        latest_tuple.3 as dest_icao,   latest_tuple.4 as arr_point_kind,
        latest_tuple.5 as win_start,
        -- kept flight-plan-class messages carry igtd but NO eta, so cap the match window off departure.
        coalesce(latest_tuple.6, latest_tuple.5 + toIntervalHour({{ var('swim_max_flight_hours') }})) as win_end,
        upper(trimBoth(latest_tuple.7)) as callsign
    from latest
),
-- Prune the (unboundedly growing) state-table scan to only what could match a swim flight: the overall
-- time span of the swim windows (PK/partition index skips pre-swim history) and the swim callsign set.
bounds as (
    select min(win_start) - toIntervalSecond({{ var('callsign_backfill_window_s') }}) as lo,
           max(win_end)   + toIntervalSecond({{ var('callsign_backfill_window_s') }}) as hi
    from flat
),
swim_callsigns as (select distinct callsign from flat),
obs as (  -- observed hex sightings by trim+UPPER-normalized callsign (both hex lanes), per the design
    select upper(trimBoth(callsign)) as cs, icao24 as hex,
           toUnixTimestamp64Micro(snapshot_time)/1e6 as epoch
    from {{ source('bronze', 'opensky_states') }}
    where callsign is not null and trimBoth(callsign) <> '' and icao24 is not null
      and snapshot_time between (select lo from bounds) and (select hi from bounds)
      and upper(trimBoth(callsign)) in (select callsign from swim_callsigns)
    union all
    select upper(trimBoth(flight)) as cs, hex,
           capture_ts as epoch     -- adsb_states.capture_ts is already Float64 epoch seconds
    from {{ source('bronze', 'adsb_states') }}
    where flight is not null and trimBoth(flight) <> '' and hex is not null
      and capture_ts between toUnixTimestamp((select lo from bounds)) and toUnixTimestamp((select hi from bounds))
      and upper(trimBoth(flight)) in (select callsign from swim_callsigns)
),
scored as (  -- density = count of matching sightings in the filed window, per candidate hex
    select f.flight_key, o.hex, count(*) as score
    from flat f
    join obs o
      on o.cs = f.callsign
     and o.epoch between toUnixTimestamp(f.win_start) - {{ var('callsign_backfill_window_s') }}
                     and toUnixTimestamp(f.win_end)   + {{ var('callsign_backfill_window_s') }}
    group by f.flight_key, o.hex
),
ranked as (
    select flight_key, hex, score,
        row_number() over (partition by flight_key order by score desc, hex asc) as rn,
        -- lead over the runner-up; SAME order as rn (score desc, hex asc) so the rn=1 row's "1 following" is the
        -- true runner-up; anyOrNull → NULL only for a genuine sole candidate (→ not ambiguous).
        (max(score) over (partition by flight_key)
         - anyOrNull(score) over (partition by flight_key order by score desc, hex asc
                                  rows between 1 following and 1 following)) as lead_gap
    from scored
),
resolved as (
    select flight_key,
        -- resolve only on a clear winner (>1 sighting lead); a lead of 0 or 1 is ambiguous → withhold (NULL).
        if(rn = 1 and (lead_gap is null or lead_gap > 1), hex, null) as icao24,
        (rn = 1 and lead_gap is not null and lead_gap <= 1) as hex_ambiguous,
        score as hex_score
    from ranked where rn = 1
)
select f.flight_key, r.icao24, f.win_start, f.win_end, f.callsign,
       f.origin_icao, f.dest_icao, f.dep_point_kind, f.arr_point_kind,
       ifNull(r.hex_score, 0) as hex_score, ifNull(r.hex_ambiguous, 0) as hex_ambiguous
from flat f
left join resolved r using (flight_key)
