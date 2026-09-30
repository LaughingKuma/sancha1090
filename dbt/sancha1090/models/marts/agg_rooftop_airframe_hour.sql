{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    engine='MergeTree()',
    order_by=['hour_utc', 'hex'],
    partition_by='capture_date',
    query_settings={'max_memory_usage': 12000000000, 'max_partitions_per_insert_block': 10000},
    tags=['adsb'],
) }}
{#- One cap for every path: config renders at parse time and the first build (all history, ~6.1 GiB, every day
    in one insert block) is not a --full-refresh, so a flag-keyed small cap would fail it. #}

{%- set w = adsb_rebuild_window() %}
{%- do adsb_partition_key_guard('capture_date') %}

-- Rebuilds the window plus older days whose fix sum no longer matches bronze:
-- docs/notes/runbooks.md#callsign-backfill-repair
{%- if w.windowed %}{% set bd = adsb_build_days(w, count_against='self') %}{% endif %}
with
per_minute as (
    select
        capture_date,
        toStartOfMinute(toDateTime(assumeNotNull(capture_ts), 'UTC')) as minute_utc,
        assumeNotNull(lower(trimBoth(hex))) as hex,
        argMaxIf(t, capture_ts, ifNull(t, '') != '') as typecode,
        count() as n,
        groupUniqArrayIf(upper(trimBoth(flight)), trimBoth(ifNull(flight, '')) != '') as callsigns,
        max(bitAnd(db_flags, 8) != 0) as flag_ladd,
        max(bitAnd(db_flags, 1) != 0) as flag_mil,
        -- the live map's is_helicopter test (risingwave/sql/03_mv_current_aircraft.sql)
        max(ifNull(category, '') = 'A7') as flag_heli,
        median(toFloat64OrNull(alt_baro)) as alt_med,
        median(gs) as gs_med,
        median(r_dst) as dst_med,
        argMinIf(tuple(lat, lon, capture_ts), capture_ts, lat is not null and lon is not null) as p0,
        argMaxIf(tuple(lat, lon, capture_ts), capture_ts, lat is not null and lon is not null) as p1
    from {{ source('bronze', 'adsb_states') }}
    where hex is not null and capture_ts is not null
    {%- if w.windowed %}
      and {{ adsb_day_in(bd, 'capture_date') }}
      {%- if bd.prune_ts %}
      -- the same bounds on the primary key prune the scan to the window's granules, not the whole month
      and capture_ts >= toUnixTimestamp(toDateTime('{{ bd.lo }}', 'UTC'))
      and capture_ts <  toUnixTimestamp(toDateTime('{{ bd.hi_excl }}', 'UTC'))
      {%- endif %}
    {%- endif %}
    group by capture_date, minute_utc, hex
)
select
    toStartOfHour(minute_utc) as hour_utc,
    capture_date,
    hex,
    argMaxIf(typecode, minute_utc, typecode is not null) as typecode,
    sum(n) as n_fixes,
    arraySort(groupUniqArrayArray(callsigns)) as callsigns,
    max(flag_ladd) as flag_ladd,
    max(flag_mil) as flag_mil,
    max(flag_heli) as flag_heli,
    arraySort(groupArray(toUInt8(toMinute(minute_utc)))) as minutes,
    -- records take only minutes with >= 5 fixes, so one bad decode cannot move a per-minute median
    maxIf(alt_med, n >= 5) as alt_rec_ft,
    -- a median gs must agree with the minute's first-to-last position speed: drops repeated bad-decode speeds
    maxIf(gs_med, n >= 5 and p1.3 - p0.3 >= 20
          and abs(greatCircleDistance(p0.2, p0.1, p1.2, p1.1) / 1852 / ((p1.3 - p0.3) / 3600) - gs_med)
              <= 0.1 * gs_med) as gs_rec_kt,
    -- beyond the radio horizon for that altitude (plus a 1000 ft antenna term) the position is a bad decode
    maxIf(dst_med, n >= 5 and alt_med > 0 and dst_med <= 1.23 * (sqrt(alt_med) + sqrt(1000))) as dst_rec_nmi
from per_minute
group by hour_utc, capture_date, hex
