{{ config(
    materialized='table',
    tags=['swim'],
    query_settings={'max_memory_usage': 4000000000},
) }}

-- SWIM has no Mode-S hex: resolve icao24 by DENSITY of observed callsign-matched snapshots in the filed window,
-- suppressing ambiguous top-two to NULL; latest-amendment rows come from the physical int_swim_latest (#187).
-- Prune the (unboundedly growing) state-table scan to only what could match a swim flight: the overall
-- time span of the swim windows (PK/partition index skips pre-swim history) and the swim callsign set.
with bounds as (
    select min(win_start) - toIntervalSecond({{ var('callsign_backfill_window_s') }}) as lo,
           max(win_end)   + toIntervalSecond({{ var('callsign_backfill_window_s') }}) as hi
    from {{ ref('int_swim_latest') }}
),
swim_callsigns as (select distinct callsign from {{ ref('int_swim_latest') }}),
obs as (  -- observed hex sightings by trim+UPPER-normalized callsign (both hex lanes), per the design
    -- lane tags each arm so scored can dedup RMT un-merged duplicates by (lane, epoch) instead of raw count.
    select 'os' as lane, upper(trimBoth(callsign)) as cs, icao24 as hex,
           toUnixTimestamp64Micro(snapshot_time)/1e6 as epoch
    from {{ source('bronze', 'opensky_states') }}
    where callsign is not null and trimBoth(callsign) <> '' and icao24 is not null
      and snapshot_time between (select lo from bounds) and (select hi from bounds)
      and upper(trimBoth(callsign)) in (select callsign from swim_callsigns)
    union all
    select 'adsb' as lane, upper(trimBoth(flight)) as cs, hex,
           capture_ts as epoch     -- adsb_states.capture_ts is already Float64 epoch seconds
    from {{ source('bronze', 'adsb_states') }}
    where flight is not null and trimBoth(flight) <> '' and hex is not null
      and capture_ts between toUnixTimestamp((select lo from bounds)) and toUnixTimestamp((select hi from bounds))
      and upper(trimBoth(flight)) in (select callsign from swim_callsigns)
),
scored as (  -- density = distinct (lane, epoch) sightings, dedup-immune to RMT un-merged duplicates
    select f.flight_key, o.hex, uniqExact(o.lane, o.epoch) as score
    from {{ ref('int_swim_latest') }} f
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
from {{ ref('int_swim_latest') }} f
left join resolved r using (flight_key)
