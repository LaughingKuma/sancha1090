{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    engine='MergeTree()',
    partition_by='day_key',
    query_settings={'max_memory_usage': 12000000000 if flags.FULL_REFRESH else 2000000000},
    tags=['adsb'],
) }}

{%- set rebuild_to = var('callsign_backfill_rebuild_to', '') | string %}
{%- set rebuild_days = var('callsign_backfill_rebuild_days') %}
{#- `| int` would let 1.5 or true pass the check and still reach the SQL as written. #}
{%- if rebuild_days is boolean or rebuild_days is not integer or rebuild_days < 1 %}
{{ exceptions.raise_compiler_error("callsign_backfill_rebuild_days must be a whole number >= 1, got " ~ rebuild_days) }}
{%- endif %}
{%- if rebuild_to %}
{#- toDateOrNull would let a typo like 2026-06-31 fall back to the newest days and repair the wrong partitions. #}
{%- do modules.datetime.datetime.strptime(rebuild_to, '%Y-%m-%d') %}
{%- if flags.FULL_REFRESH %}
{{ exceptions.raise_compiler_error("--full-refresh always recomputes all history; to rebuild in slices drop the table first, then run with the repair vars") }}
{%- endif %}
{%- endif %}
{#- A rebuild is windowed on the incremental path and whenever a slice is nominated by hand. #}
{%- set windowed = is_incremental() or rebuild_to != '' %}
{%- if is_incremental() %}
{#- REPLACE PARTITION needs matching partition keys: against the pre-incremental unpartitioned table it fails only
    after creating the __dbt_new_data table, leaving one orphan per tick -- fail before any statement instead. #}
{%- set partition_key = run_query("select partition_key from system.tables where database = '" ~ this.schema ~ "' and name = '" ~ this.identifier ~ "'").columns[0].values() %}
{%- if partition_key[0] | replace('(', '') | replace(')', '') | trim != 'day_key' %}
{{ exceptions.raise_compiler_error(this ~ " is not partitioned by day_key: pause transform_adsb_silver and run this model once with --full-refresh") }}
{%- endif %}
{%- endif %}

-- ADS-B identity messages broadcast ~10x less often than position, so ~4% of frames land with a decoded
-- position but a blank callsign (worse at the range edge, where the rarer ID frame fails CRC). The same
-- airframe is in the OpenSky context feed within seconds; take the nearest OpenSky callsign inside the
-- backfill window. row_number()=1 keeps this single-valued per (hex, capture_ts) so the LEFT join into
-- fct_adsb_state stays row-count-preserving.
-- A range-join + row_number nearest-abs is exact but the heaviest op in the lane (5.7 GiB in the
-- spike). A TWO-SIDED ASOF is exact AND cheap: one ASOF picks the nearest *preceding* OpenSky snapshot,
-- one the nearest *following*; row_number()=1 over abs(d) then picks the closer of the two -- the true
-- nearest is necessarily one of them. (A single preceding-only ASOF under-fills by 18%, so both sides
-- are required for parity.) snapshot_time is DateTime64(6) -> micro-epoch seconds to match to_unixtime.
-- Incremental by UTC day (#193): a full-history rebuild carries every callsign-bearing OpenSky row as the ASOF build
-- side (~6 GB, +0.04 GB/day). Only the trailing window rebuilds; replayed bronze older than it needs the repair vars
-- (docs/notes/runbooks.md#callsign-backfill-repair).
with
{%- if windowed %}
build_days as (
    -- watermark, not now(): a stalled feed keeps rebuilding its last real days instead of empty ones
    select day_hi - {{ rebuild_days }} + 1 as day_lo, day_hi
    from (
        {%- if rebuild_to %}
        select toDate('{{ rebuild_to }}') as day_hi
        {%- else %}
        select max(capture_date) as day_hi from {{ source('bronze', 'adsb_states') }}
        {%- endif %}
    )
),
{%- endif %}
miss as (
    select distinct hex, capture_ts, capture_date as day_key
    from {{ source('bronze', 'adsb_states') }}
    where (flight is null or trimBoth(flight) = '')
    {%- if windowed %}
      and capture_date between (select day_lo from build_days) and (select day_hi from build_days)
      -- the same bounds on the primary key prune the scan to the window's granules, not the whole month
      and capture_ts >= toUnixTimestamp(toDateTime((select day_lo from build_days), 'UTC'))
      and capture_ts <  toUnixTimestamp(toDateTime((select day_hi from build_days) + 1, 'UTC'))
    {%- endif %}
),
opensky as (
    select icao24, toUnixTimestamp64Micro(snapshot_time) / 1e6 as snap_epoch, trimBoth(callsign) as callsign
    from {{ source('bronze', 'opensky_states') }}
    where callsign is not null and trimBoth(callsign) <> ''
    {%- if windowed %}
      -- the +/- window straddles midnight, so a frame at 00:05 still needs the 23:55 snapshot
      and snapshot_time >= toDateTime64((select day_lo from build_days), 6, 'UTC') - interval {{ var('callsign_backfill_window_s') }} second
      and snapshot_time <  toDateTime64((select day_hi from build_days) + 1, 6, 'UTC') + interval {{ var('callsign_backfill_window_s') }} second
    {%- endif %}
),
preceding as (
    select m.hex, m.capture_ts, m.day_key, o.callsign, o.snap_epoch, (m.capture_ts - o.snap_epoch) as d
    from miss m
    asof left join opensky o
      on o.icao24 = m.hex and o.snap_epoch <= m.capture_ts
    where o.callsign is not null and (m.capture_ts - o.snap_epoch) <= {{ var('callsign_backfill_window_s') }}
),
following as (
    select m.hex, m.capture_ts, m.day_key, o.callsign, o.snap_epoch, (o.snap_epoch - m.capture_ts) as d
    from miss m
    asof left join opensky o
      on o.icao24 = m.hex and o.snap_epoch >= m.capture_ts
    where o.callsign is not null and (o.snap_epoch - m.capture_ts) <= {{ var('callsign_backfill_window_s') }}
),
nearest as (
    select
        hex, capture_ts, day_key, callsign,
        row_number() over (
            partition by hex, capture_ts
            -- nearest wins; later snapshot then callsign break ties (deterministic tie-break order).
            order by abs(d) asc, snap_epoch desc, callsign asc
        ) as rn
    from (select * from preceding union all select * from following)
)
select hex, capture_ts, day_key, callsign as filled_callsign
from nearest
where rn = 1
