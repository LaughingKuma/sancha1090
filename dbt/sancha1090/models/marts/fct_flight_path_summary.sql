{{ config(
    materialized='incremental',
    incremental_strategy='insert_overwrite',
    engine='MergeTree()',
    order_by=['flight_id'],
    partition_by='day_key',
    query_settings={
        'max_memory_usage': 16000000000 if flags.FULL_REFRESH else 2000000000,
        'max_partitions_per_insert_block': 10000,
    },
) }}

{%- set parent = ref('fct_flight_path') %}
{%- if is_incremental() %}
{#- REPLACE PARTITION needs matching partition keys: against the pre-incremental unpartitioned table it fails only
    after creating the __dbt_new_data table, leaving one orphan per tick -- fail before any statement instead. #}
{%- set partition_key = run_query("select partition_key from system.tables where database = '" ~ this.schema ~ "' and name = '" ~ this.identifier ~ "'").columns[0].values() %}
{%- if partition_key[0] | replace('(', '') | replace(')', '') | trim != 'day_key' %}
{{ exceptions.raise_compiler_error(this ~ " is not partitioned by day_key: pause transform_marts and run this model once with --full-refresh") }}
{%- endif %}
{%- endif %}

-- Per-flight QA row for fct_flight_path: point counts, per-source split, largest coverage gap, and
-- observed_fraction over the reconciled window -- makes honest-gap labelling queryable without scanning the
-- point table. Inner join reconciled: orphaned flight_ids (historical re-key) drop out when their day rebuilds.
-- n_points/n_*/largest_gap_s are whole-recorded-path (they keep the +/-10min pad fixes); observed_fraction's numerator counts only
-- fixes inside the UNPADDED [start_time, end_time] so it is not inflated by pad fixes over the unpadded window_s.
-- Partitioned by the parent's day_key and rebuilt a day at a time (#194): start_time is baked into flight_id, so a
-- summary day depends on its parent partition alone and is due exactly when that partition changed.
-- One partition per parent day: --full-refresh lands them all in one INSERT block, past ClickHouse's default 100.
with parent_parts as (
    -- newest active part per parent day: every REPLACE PARTITION (forward, orphan batch, path_repair_days) gets a fresh
    -- modification_time; a background merge does too, which re-summarises that day once more with identical output.
    select toDate(partition) as day, max(modification_time) as path_parts_mtime
    from system.parts
    where database = '{{ parent.schema }}' and table = '{{ parent.identifier }}' and active
    group by partition
),
build_days as (
{%- if is_incremental() %}
    -- Due = fingerprint missing or different from the day's summary rows; the state is the summary itself, so a failed
    -- run leaves the same days due next tick. Newest first under a cap that binds only while a dropped table drains.
    select p.day
    from parent_parts p
    left join (
        select day_key as day, max(path_parts_mtime) as seen
        from {{ this }}
        group by day_key
    ) s on s.day = p.day
    where (s.seen is null or s.seen != p.path_parts_mtime)
      -- a day with no current spine start would summarise to zero rows, REPLACE nothing and stay due forever
      and p.day in (select toDate(start_time) from {{ ref('fct_flights_reconciled') }})
    order by p.day desc
    limit {{ var('path_summary_chunk_days') }}
{%- elif flags.FULL_REFRESH %}
    select day from parent_parts
{%- else %}
    -- first build with no table: newest chunk only, the lane drains the rest newest-first
    select day from parent_parts order by day desc limit {{ var('path_summary_chunk_days') }}
{%- endif %}
),
pts as (
    select p.flight_id, p.day_key, p.ts, p.source, r.start_time, r.end_time
    from {{ parent }} p
    join {{ ref('fct_flights_reconciled') }} r on r.flight_id = p.flight_id
    where p.day_key in (select day from build_days)
      -- day_key = toDate(start_time) by construction, so the join's build side prunes to the same days
      and toDate(r.start_time) in (select day from build_days)
),
agg as (
    -- start_time/end_time are constant per flight_id -> group by them too so they're usable (not aggregates)
    -- in the in-window countIf and window_s.
    select
        flight_id,
        day_key,
        count() as n_points,
        countIf(source = 'adsb') as n_adsb,
        countIf(source = 'adsblol') as n_adsblol,
        countIf(source = 'opensky') as n_opensky,
        min(ts) as first_fix_ts,
        max(ts) as last_fix_ts,
        -- consecutive-ts deltas over the sorted second-grain fixes; single-point flights yield 0.
        toUInt32(arrayMax(arrayDifference(arraySort(groupArray(toUnixTimestamp(ts)))))) as largest_gap_s,
        countIf(ts between start_time and end_time) as n_in_window,
        toUInt32(dateDiff('second', start_time, end_time)) as window_s
    from pts
    group by flight_id, day_key, start_time, end_time
)
select
    a.flight_id as flight_id,
    a.n_points as n_points,
    a.n_adsb as n_adsb,
    a.n_adsblol as n_adsblol,
    a.n_opensky as n_opensky,
    a.first_fix_ts as first_fix_ts,
    a.last_fix_ts as last_fix_ts,
    a.largest_gap_s as largest_gap_s,
    a.window_s as window_s,
    if(a.window_s = 0, toFloat64(0), least(toFloat64(1), a.n_in_window / a.window_s)) as observed_fraction,
    a.day_key as day_key,
    pp.path_parts_mtime as path_parts_mtime
from agg a
join parent_parts pp on pp.day = a.day_key
