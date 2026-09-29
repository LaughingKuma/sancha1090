{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    engine='MergeTree()',
    partition_by='day_key',
    query_settings={'max_memory_usage': 12000000000 if flags.FULL_REFRESH else 2000000000,
                    'max_partitions_per_insert_block': 10000},
    tags=['adsb'],
) }}
{#- one partition per UTC day, so --full-refresh lands every day in a single insert block (default cap 100) #}

{%- set w = adsb_rebuild_window() %}
{%- do adsb_partition_key_guard('day_key') %}

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
-- side (~6 GB, +0.04 GB/day). The trailing window rebuilds, plus older days whose bronze count drifted from
-- fct_adsb_state; an OpenSky replay needs the repair vars (docs/notes/runbooks.md#callsign-backfill-repair).
{%- if w.windowed %}{% set bd = adsb_build_days(w) %}{% endif %}
with
miss as (
    select distinct hex, capture_ts, capture_date as day_key
    from {{ source('bronze', 'adsb_states') }}
    where (flight is null or trimBoth(flight) = '')
    {%- if w.windowed %}
      and {{ adsb_day_in(bd, 'capture_date') }}
      {%- if bd.prune_ts %}
      -- the same bounds on the primary key prune the scan to the window's granules, not the whole month
      and capture_ts >= toUnixTimestamp(toDateTime('{{ bd.lo }}', 'UTC'))
      and capture_ts <  toUnixTimestamp(toDateTime('{{ bd.hi_excl }}', 'UTC'))
      {%- endif %}
    {%- endif %}
),
opensky as (
    select icao24, toUnixTimestamp64Micro(snapshot_time) / 1e6 as snap_epoch, trimBoth(callsign) as callsign
    from {{ source('bronze', 'opensky_states') }}
    where callsign is not null and trimBoth(callsign) <> ''
    {%- if w.windowed %}
      -- the +/- window straddles midnight, so a frame at 00:05 still needs the 23:55 snapshot
      and snapshot_time >= toDateTime64('{{ bd.lo }}', 6, 'UTC')
                           - interval {{ var('callsign_backfill_window_s') }} second
      and snapshot_time <  toDateTime64('{{ bd.hi_excl }}', 6, 'UTC')
                           + interval {{ var('callsign_backfill_window_s') }} second
      {%- if not bd.prune_ts %}
      -- a gappy day set spans more history than it rebuilds: keep only snapshots within the window of a set
      -- day, so the ASOF build side stays sized to the set, not the span, under the 2 GB cap
      {%- set shift = "interval " ~ var('callsign_backfill_window_s') ~ " second, 'UTC')" %}
      and ({{ adsb_day_in(bd, "toDate(snapshot_time - " ~ shift) }}
           or {{ adsb_day_in(bd, "toDate(snapshot_time + " ~ shift) }})
      {%- endif %}
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
