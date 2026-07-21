{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    engine='MergeTree()',
    order_by=['flight_id', 'ts'],
    partition_by='day_key',
    query_settings={'max_memory_usage': 16000000000},
) }}

{#- path_repair_days (manual restoration lever): contiguous sorted prefix only, capped at the chunk size — mixing dates with orphan/forward days (or a non-contiguous list) widens chunk_bounds and can breach the 16 GiB bound. #}
{%- set repair_days_raw = var('path_repair_days', []) %}
{%- set repair_batch = [] %}
{%- if repair_days_raw %}
{%- if repair_days_raw is string %}
{{ exceptions.raise_compiler_error("path_repair_days must be a list of 'YYYY-MM-DD' strings") }}
{%- endif %}
{%- set parsed_days = [] %}
{%- for d in repair_days_raw %}
{%- do parsed_days.append(modules.datetime.datetime.strptime(d | string, '%Y-%m-%d').date()) %}
{%- endfor %}
{%- for d in parsed_days | sort %}
{%- if repair_batch | length < var('path_build_chunk_days')
      and (repair_batch | length == 0 or (d - repair_batch[-1]).days == 1) %}
{%- do repair_batch.append(d) %}
{%- endif %}
{%- endfor %}
{%- endif %}

-- Fused per-second trajectory per reconciled flight: priority union rooftop(adsb) > adsblol > opensky.
-- Daily partitions are replaced atomically, not appended. START day remains the partition key because it is
-- stable for one flight_id; replacement is what also removes an OLD id when the same physical flight re-anchors.
-- A new day becomes eligible at max(trace_day) - path_settlement_lag_days; under lag 1 the 14:30 UTC OpenSky
-- anchor waves land AFTER their days first build BY DESIGN, and the oldest-contiguous orphan batch repairs the
-- re-keyed days in the same run that rebuilt the spine, before dbt test gates. Whenever the high-water mark
-- advances, the newest path_build_chunk_days partitions are rebuilt, so late path loads and recent spine churn
-- self-heal. During a historical catch-up, the same bounded chunk size advances oldest-first.
-- Non-contiguous remainders red the FK gate until they drain on later runs; an adjacent-day re-key outside
-- the nominated set waits for that day's own rebuild.
-- LIMIT BY sorts in memory and does not spill; five dense days fit below the 16 GB query backstop.
-- Operators: changing the partition key from the pre-v6.25 monthly layout requires one paused --full-refresh.
-- An unfixable orphan partition (no current-spine start; stays red on assert_flight_path_reconciled_fk) is
-- removed manually: ALTER TABLE gold_ch.fct_flight_path DROP PARTITION '<day>'. A targeted rebuild instead
-- forces days via --vars path_repair_days=['<day>',...]; DROP PARTITION remains only for truly unfixable orphans.
with source_floor as (
    select least(
        (select min(capture_date) from {{ source('bronze', 'adsb_states') }}),
        (select min(trace_day) from {{ source('bronze', 'adsblol_flight_paths') }}),
        (select min(snapshot_date) from {{ source('bronze', 'opensky_states') }})) as day
),
eligible_days as (
    select toDate(start_time) as day
    from {{ ref('fct_flights_reconciled') }}
    where toDate(start_time) <= (select max(trace_day) from {{ source('bronze', 'adsblol_flight_paths') }})
                                  - {{ var('path_settlement_lag_days') }}
      -- A <24h flight can start the day before the first fix-source partition and still overlap it.
      and toDate(start_time) >= (select day from source_floor) - 1
    group by toDate(start_time)
),
eligible_state as (
    select coalesce(max(day), toDate('1970-01-01')) as eligible_hi from eligible_days
),
{%- if is_incremental() %}
{%- if repair_batch %}
chunk_days as (
    -- Manual targeted repair: EXCLUSIVE of orphan/forward nomination for this run (see header Jinja note).
    select toDate(day) as day
    from (select arrayJoin([{% for d in repair_batch %}'{{ d.isoformat() }}'{{ ", " if not loop.last }}{% endfor %}]) as day)
),
{%- else %}
path_state as (
    select coalesce(max(day_key), toDate('1970-01-01')) as built_hi from {{ this }}
),
stale_days as (
    -- Lag-1 anchor waves stale up to two built days at once: nominate day-grain DISTINCT so duplicate path
    -- rows never consume the cap; a no-current-start day skips nomination (zero-row rebuild never REPLACEs).
    select distinct p.day_key as day
    from {{ this }} p
    left anti join {{ ref('fct_flights_reconciled') }} r on r.flight_id = p.flight_id
    where p.day_key in (select toDate(start_time) from {{ ref('fct_flights_reconciled') }})
),
orphan_days as (
    -- Oldest contiguous run only, capped: the wave's adjacent days repair in ONE run before dbt_test_ch;
    -- non-contiguous days would widen chunk_bounds across the gap, so remainders drain on later runs.
    select day
    from (select day, row_number() over (order by day) as rn, min(day) over () as day0 from stale_days)
    where dateDiff('day', day0, day) = rn - 1
    order by day
    limit {{ var('path_build_chunk_days') }}
),
forward_days as (
    select e.day
    from eligible_days e
    cross join eligible_state s
    cross join path_state p
    where not exists (select 1 from orphan_days)
      and s.eligible_hi > p.built_hi
      and (
        -- Large gap = bounded oldest-first catch-up. Near head = replace the full rolling repair window.
        (dateDiff('day', p.built_hi, s.eligible_hi) > {{ var('path_build_chunk_days') }} and e.day > p.built_hi)
        or
        (dateDiff('day', p.built_hi, s.eligible_hi) <= {{ var('path_build_chunk_days') }}
         and e.day >= s.eligible_hi - ({{ var('path_build_chunk_days') }} - 1))
      )
    order by day
    limit {{ var('path_build_chunk_days') }}
),
chunk_days as (
    select day from orphan_days
    union all
    select day from forward_days
),
{%- endif %}
{%- else %}
{#- Guard is execute-gated on the non-incremental branch: dbt renders twice and is_incremental() is False
    during the parse pass (execute=False), so an ungated check false-trips every repair run at compile. #}
{%- if execute and repair_batch %}
{{ exceptions.raise_compiler_error("path_repair_days requires the existing incremental table (no --full-refresh / first build)") }}
{%- endif %}
chunk_days as (
    select day from eligible_days
    order by day
    limit {{ var('path_build_chunk_days') }}
),
{%- endif %}
chunk_bounds as (
    -- bronze scan window for the chunk's fixes: the emit (start-day) flights' padded windows, +/-1 day margin.
    select
        min(toDate(toDateTime(start_time, 'UTC') - interval {{ var('path_window_pad_min') }} minute)) - 1 as day_lo,
        max(toDate(toDateTime(end_time,   'UTC') + interval {{ var('path_window_pad_min') }} minute)) + 1 as day_hi
    from {{ ref('fct_flights_reconciled') }}
    where toDate(start_time) in (select day from chunk_days)
),
spine as (
    -- Stage-1 contest set: emit flights (in_chunk = start day in this chunk) PLUS shadow contestants -- any
    -- flight whose padded window overlaps the scan range, so nearest-window assignment matches single-shot
    -- semantics and a boundary fix is never emitted under two flights across chunks. Window <= 16h+pad < 1 day,
    -- so overlap = start <= day_hi+1 AND end >= day_lo-1. Only in_chunk rows are emitted at the end.
    select
        flight_id,
        lower(icao24) as icao24,
        toDateTime(start_time, 'UTC') - interval {{ var('path_window_pad_min') }} minute as win_start,
        toDateTime(end_time,   'UTC') + interval {{ var('path_window_pad_min') }} minute as win_end,
        intDiv(toInt64(toUnixTimestamp(start_time)) + toInt64(toUnixTimestamp(end_time)), 2) as win_mid_s,
        toDate(assumeNotNull(start_time)) as day_key,
        toDate(start_time) in (select day from chunk_days) as in_chunk
    from {{ ref('fct_flights_reconciled') }}
    where toDate(start_time) <= (select day_hi from chunk_bounds) + 1
      and toDate(end_time)   >= (select day_lo from chunk_bounds) - 1
),

{#- Bronze scans prune to the chunk's fix days (from the emit flights only, not the shadow set). #}
{%- set day_lo = "(select day_lo from chunk_bounds)" %}
{%- set day_hi = "(select day_hi from chunk_bounds)" %}

adsb_fixes as (
    -- rooftop: capture_ts Float64 epoch -> whole second; alt_baro STRING with a 'ground' sentinel.
    select
        cast('adsb' as LowCardinality(String)) as source,
        toUInt8(1) as src_rank,
        lower(hex) as icao24,
        toDateTime(toUInt32(floor(assumeNotNull(capture_ts))), 'UTC') as ts,
        lat, lon,
        if(alt_baro = 'ground', 0, toFloat64OrNull(alt_baro)) as alt_ft,
        toUInt8(coalesce(alt_baro, '') = 'ground') as on_ground,
        gs as gs_kt,
        track as track_deg
    from {{ source('bronze', 'adsb_states') }}
    where capture_date between {{ day_lo }} and {{ day_hi }}
      and capture_ts is not null
      and lat is not null and lon is not null
),
adsblol_fixes as (
    -- adsb.lol full trace: already ft/kt and int-second. RMT stale-row residue is harmless here -- the
    -- fusion dedup absorbs it, so no FINAL.
    select
        cast('adsblol' as LowCardinality(String)) as source,
        toUInt8(2) as src_rank,
        lower(icao24) as icao24,
        toDateTime(assumeNotNull(ts), 'UTC') as ts,
        lat, lon,
        alt_ft,
        toUInt8(coalesce(on_ground, false)) as on_ground,
        gs_kt,
        track_deg
    from {{ source('bronze', 'adsblol_flight_paths') }}
    where trace_day between {{ day_lo }} and {{ day_hi }}
      and ts is not null
      and lat is not null and lon is not null
),
opensky_fixes as (
    -- OpenSky states: baro metres -> ft, velocity m/s -> kt; ts = time_position, else snapshot_time.
    select
        cast('opensky' as LowCardinality(String)) as source,
        toUInt8(3) as src_rank,
        lower(icao24) as icao24,
        toDateTime(assumeNotNull(coalesce(time_position, snapshot_time)), 'UTC') as ts,
        latitude as lat, longitude as lon,
        baro_altitude * 3.28084 as alt_ft,
        toUInt8(coalesce(on_ground, false)) as on_ground,
        velocity * 1.94384 as gs_kt,
        true_track as track_deg
    from {{ source('bronze', 'opensky_states') }}
    where snapshot_date between {{ day_lo }} and {{ day_hi }}
      and coalesce(time_position, snapshot_time) is not null
      and latitude is not null and longitude is not null
),
candidates as (
    select * from adsb_fixes
    union all
    select * from adsblol_fixes
    union all
    select * from opensky_fixes
),
assigned as (
    -- Stage 1: each fix goes to exactly ONE flight across the whole contest set (chunk + shadow) -- nearest
    -- window midpoint, flight_id as the total-order tiebreak. LIMIT BY, not a windowed WHERE: CH rejects a
    -- window-alias filter in the outer query, and re-evaluated scans need this order total anyway (d335ab5).
    select
        s.flight_id as flight_id,
        s.day_key as day_key,
        s.in_chunk as in_chunk,
        c.source, c.src_rank, c.icao24, c.ts, c.lat, c.lon, c.alt_ft, c.on_ground, c.gs_kt, c.track_deg
    from candidates c
    join spine s
      on s.icao24 = c.icao24
     and c.ts between s.win_start and s.win_end
    order by abs(toInt64(toUnixTimestamp(c.ts)) - s.win_mid_s), s.flight_id
    limit 1 by source, icao24, ts, lat, lon, alt_ft, gs_kt, track_deg, on_ground
)
-- Stage 2: keep only fixes won by an emit flight, then one row per (flight_id, ts) by source priority
-- (src_rank), full-tuple total order. Fixes won by a shadow flight are emitted in that flight's own chunk.
select
    assumeNotNull(flight_id) as flight_id,
    ts,
    assumeNotNull(lat) as lat,
    assumeNotNull(lon) as lon,
    alt_ft,
    on_ground,
    gs_kt,
    track_deg,
    source,
    day_key
from assigned
where in_chunk
order by flight_id, ts, src_rank, lat, lon, alt_ft, gs_kt, track_deg, on_ground
limit 1 by flight_id, ts
