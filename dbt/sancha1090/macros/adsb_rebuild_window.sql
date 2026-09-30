{#- Both models share the repair vars and the bronze watermark, each read at its own compile: a new UTC day
    landing between them skews one by a day, and fct's oldest day then keeps its prior backfill. #}
{% macro adsb_rebuild_window() %}
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
{%- do return({'rebuild_to': rebuild_to, 'rebuild_days': rebuild_days,
               'windowed': is_incremental() or rebuild_to != ''}) %}
{% endmacro %}


{% macro adsb_partition_key_guard(expected) %}
{%- if is_incremental() %}
{#- REPLACE PARTITION needs matching partition keys: against the pre-incremental unpartitioned table it fails only
    after creating the __dbt_new_data table, leaving one orphan per tick -- fail before any statement instead. #}
{%- set partition_key = run_query("select partition_key from system.tables where database = '" ~ this.schema ~ "' and name = '" ~ this.identifier ~ "'").columns[0].values() %}
{%- if partition_key[0] | replace('(', '') | replace(')', '') | trim != expected %}
{{ exceptions.raise_compiler_error(this ~ " is not partitioned by " ~ expected ~ ": pause transform_adsb_silver and run this model once with --full-refresh") }}
{%- endif %}
{%- endif %}
{% endmacro %}


{#- Literal 'YYYY-MM-DD' days to rebuild: the trailing window plus, on a plain incremental tick, days whose
    bronze count no longer matches the built table. prune_ts: the set is one contiguous run. #}
{#- count_against='self': with no dbt edge to fct_adsb_state, fct's mismatches show only by run order. #}
{% macro adsb_build_days(w, count_against='fct') %}
{%- set dt = modules.datetime %}
{%- if w.rebuild_to %}
{%- set day_hi = dt.datetime.strptime(w.rebuild_to, '%Y-%m-%d').date() %}
{%- elif execute %}
{#- Read once here, not per filter: one scan, and one value the whole query agrees on. #}
{%- set wm_sql = "select toString(maxOrNull(capture_date)) from " ~ source('bronze', 'adsb_states') %}
{%- set wm = run_query(wm_sql).columns[0].values()[0] %}
{%- set day_hi = dt.datetime.strptime((wm | string)[:10], '%Y-%m-%d').date() if wm is not none else none %}
{%- else %}
{%- set day_hi = dt.date(1970, 1, 1) %}
{%- endif %}
{%- if day_hi is none %}
{{- log("bronze.adsb_states is empty: " ~ this ~ " rebuilds no day and keeps every partition", info=true) }}
{#- an empty set renders `false`, so insert_overwrite replaces nothing #}
{%- do return({'days': [], 'lo': '1970-01-01', 'hi_excl': '1970-01-01', 'prune_ts': false}) %}
{%- endif %}
{#- watermark, not now(): a stalled feed keeps rebuilding its last real days instead of empty ones #}
{%- set days = [] %}
{%- for i in range(w.rebuild_days - 1, -1, -1) %}
{%- do days.append((day_hi - dt.timedelta(days=i)).isoformat()) %}
{%- endfor %}
{%- set extra = adsb_count_mismatch_days(days, count_against)
    if execute and is_incremental() and not w.rebuild_to else [] %}
{%- set all_days = (extra + days) | unique | sort %}
{%- set hi = dt.datetime.strptime(all_days[-1], '%Y-%m-%d').date() %}
{%- set hi_excl = (hi + dt.timedelta(days=1)).isoformat() %}
{%- do return({'days': all_days, 'lo': all_days[0], 'hi_excl': hi_excl, 'prune_ts': not extra}) %}
{% endmacro %}


{#- Days outside the window whose bronze count() differs from the built table's. The two silver models compare
    against fct: the callsign model builds first, so both see the same pre-build fct. #}
{% macro adsb_count_mismatch_days(window_days, against='fct') %}
{%- if against == 'self' %}
{%- set built = load_relation(this) %}
{%- else %}
{%- set node = graph.nodes.values() | selectattr('name', 'equalto', 'fct_adsb_state') | first %}
{%- set built = adapter.get_relation(database=node.database, schema=node.schema, identifier=node.alias) %}
{%- endif %}
{%- if built is none %}{% do return([]) %}{% endif %}
{#- no FINAL: the models read bronze without it, so a built day matches bronze's raw count #}
{%- set sql %}
select d, sumIf(n, src = 'bronze') as bronze_rows, sumIf(n, src = 'built') as built_rows
from (
{%- if against == 'self' %}
    {#- the rooftop rollup's own row predicate and fix count, so a fully built day compares equal #}
    select toString(capture_date) as d, count() as n, 'bronze' as src
    from {{ source('bronze', 'adsb_states') }}
    where hex is not null and capture_ts is not null group by capture_date
    union all
    select toString(capture_date), sum(n_fixes), 'built' from {{ built }} group by capture_date
{%- else %}
    select toString(capture_date) as d, count() as n, 'bronze' as src
    from {{ source('bronze', 'adsb_states') }} group by capture_date
    union all
    select partition, sum(rows), 'built' from system.parts
    where active and database = '{{ built.schema }}' and table = '{{ built.identifier }}' group by partition
{%- endif %}
)
where d not in ('{{ window_days | join("', '") }}')
group by d having bronze_rows != built_rows order by d
{%- endset %}
{%- set cap = 5 %}
{%- set extra = [] %}
{%- for row in run_query(sql).rows %}
{%- if row[1] | int == 0 %}
{#- a rebuild yields no rows, so REPLACE never fires: dropping the partition stays manual #}
{{- log(this ~ ": bronze has no rows for " ~ row[0] ~ " but " ~ built ~ " has " ~ row[2]
        ~ "; drop that partition by hand (runbook: callsign-backfill-repair)", info=true) }}
{%- else %}
{%- do extra.append(row[0]) %}
{%- endif %}
{%- endfor %}
{%- if extra %}
{{- log(this ~ ": rebuilding count-mismatched days " ~ extra[:cap] | join(', '), info=true) }}
{%- endif %}
{%- if extra | length > cap %}
{{- log(this ~ ": deferred to a later tick " ~ extra[cap:] | join(', '), info=true) }}
{%- endif %}
{%- do return(extra[:cap]) %}
{% endmacro %}


{% macro adsb_day_in(b, col) -%}
{%- if b.days -%}
{{ col }} in ('{{ b.days | join("', '") }}')
{%- else -%}
false
{%- endif %}
{%- endmacro %}
