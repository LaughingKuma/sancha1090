from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DBT = REPO / "dbt" / "sancha1090"
MACROS = DBT / "macros" / "adsb_rebuild_window.sql"
CALLSIGN = DBT / "models" / "silver" / "int_adsb_callsign_from_opensky.sql"
FCT = DBT / "models" / "silver" / "fct_adsb_state.sql"


def test_window_logic_has_one_home():
    # Both models must take the window from the macros, or a repair run naming both rebuilds different days.
    for model, key in ((CALLSIGN, "day_key"), (FCT, "capture_date")):
        src = model.read_text()
        assert "adsb_rebuild_window()" in src, f"{model.name} does not call adsb_rebuild_window()"
        assert f"adsb_partition_key_guard('{key}')" in src, f"{model.name} lost its partition guard"
        assert "{%- if w.windowed %}{% set bd = adsb_build_days(w) %}{% endif %}" in src
        # the callsign model builds first: a ref() to fct would be a cycle, so the macro finds fct via graph
        assert "ref('fct_adsb_state')" not in src
        assert "callsign_backfill_rebuild_to" not in src, f"{model.name} re-reads the repair vars itself"


def test_repair_vars_are_validated_once():
    src = MACROS.read_text()
    assert re.search(r"rebuild_days is boolean or rebuild_days is not integer or rebuild_days < 1", src)
    assert "strptime(rebuild_to, '%Y-%m-%d')" in src
    # --full-refresh recomputes all history, so a slice on top of it would silently repair the wrong days.
    assert re.search(r"if rebuild_to.*?if flags\.FULL_REFRESH", src, re.DOTALL)
    assert src.count("raise_compiler_error") == 3


def test_watermark_is_read_once_at_compile():
    # A max() inside build_days re-scans bronze in every scalar subquery, each free to see a different value.
    src = MACROS.read_text()
    days = src[src.index("macro adsb_build_days(w)"):src.index("macro adsb_count_mismatch_days")]
    assert re.search(
        r"elif execute %\}.*?\"select toString\(maxOrNull\(capture_date\)\) from \" ~ "
        r"source\('bronze', 'adsb_states'\).*?run_query\(wm_sql\)", days, re.DOTALL
    )
    assert "{%- do days.append((day_hi - dt.timedelta(days=i)).isoformat()) %}" in days
    # empty bronze: an empty set renders `false`, so insert_overwrite replaces no partition
    assert (
        "{%- do return({'days': [], 'lo': '1970-01-01', 'hi_excl': '1970-01-01', "
        "'prune_ts': false}) %}" in days
    )
    day_in = src[src.index("macro adsb_day_in(b, col)"):]
    assert """{{ col }} in ('{{ b.days | join("', '") }}')""" in day_in
    assert re.search(r"\{%- else -%\}\s*\nfalse\s*\n", day_in)


def test_count_mismatch_days_are_added_only_on_the_plain_incremental_tick():
    # The manual repair path stays exclusive and --full-refresh already rebuilds every day.
    src = MACROS.read_text()
    days = src[src.index("macro adsb_build_days(w)"):src.index("macro adsb_count_mismatch_days")]
    assert (
        "{%- set extra = adsb_count_mismatch_days(days) if execute and is_incremental() "
        "and not w.rebuild_to else [] %}" in days
    )
    assert "{%- set all_days = (extra + days) | unique | sort %}" in days
    # a gappy set drops the capture_ts bounds: NULL capture_ts rows sit on 1970-01-01, still rebuildable
    assert "'prune_ts': not extra}" in days


def test_count_mismatch_compares_bronze_with_fct_partitions():
    src = MACROS.read_text()
    m = src[src.index("macro adsb_count_mismatch_days"):src.index("macro adsb_day_in")]
    # both models compare against fct, found without ref() so the callsign model gains no cycle
    assert "selectattr('name', 'equalto', 'fct_adsb_state')" in m
    assert "adapter.get_relation(database=node.database, schema=node.schema, identifier=node.alias)" in m
    assert "{%- if fct is none %}{% do return([]) %}{% endif %}" in m
    # no FINAL: the models read bronze without it, so counts compare like for like
    assert "final" not in m.lower().split("{%- set sql %}")[1]
    assert re.search(
        r"select toString\(capture_date\) as d, count\(\) as n, 'bronze' as src\s*\n"
        r"\s*from \{\{ source\('bronze', 'adsb_states'\) \}\} group by capture_date", m
    )
    assert "select partition, sum(rows), 'fct' from system.parts" in m
    assert "where active and database = '{{ fct.schema }}' and table = '{{ fct.identifier }}'" in m
    assert """where d not in ('{{ window_days | join("', '") }}')""" in m
    assert "group by d having bronze_rows != fct_rows order by d" in m
    # bronze-empty days never REPLACE, so they are logged for a manual drop instead of nominated
    assert re.search(r"if row\[1\] \| int == 0 %\}.*?log\(.*?\{%- else %\}\s*\n\{%- do extra\.append", m,
                     re.DOTALL)
    # oldest first, 5 per build: 10 of the oldest days peak at 1.67 GiB of the callsign model's 2 GB cap
    assert "{%- set cap = 5 %}" in m
    assert "extra[cap:]" in m and "{%- do return(extra[:cap]) %}" in m


def test_partition_guard_fails_before_any_statement():
    # Against an unpartitioned table REPLACE PARTITION fails only after creating __dbt_new_data, leaving
    # one orphan per tick; the guard reads system.tables and raises at compile instead.
    src = MACROS.read_text()
    assert re.search(
        r"is_incremental\(\)\s*%\}.*?run_query\(\"select partition_key from system\.tables", src, re.DOTALL
    )
    assert "run this model once with --full-refresh" in src
    for model in (CALLSIGN, FCT):
        s = model.read_text()
        # the SQL's own `with` line, not the word inside a header comment
        sql_start = re.search(r"^with\s*$", s, re.M).start()
        assert s.index("adsb_partition_key_guard(") < sql_start, f"{model.name} guards too late"


def test_fct_adsb_state_is_partitioned_incremental():
    src = FCT.read_text()
    assert "materialized='incremental'" in src
    assert "incremental_strategy='insert_overwrite'" in src
    assert "partition_by='capture_date'" in src
    assert "tags=['adsb']" in src


def test_full_refresh_lifts_the_partition_cap_in_both_models():
    # Every UTC-day partition lands in one insert block on --full-refresh; the server default caps at 100.
    for model in (CALLSIGN, FCT):
        assert "'max_partitions_per_insert_block': 10000" in model.read_text(), f"{model.name} lacks the cap"
    assert "'max_memory_usage': 12000000000 if flags.FULL_REFRESH else 2000000000" in CALLSIGN.read_text()


def test_fct_adsb_state_windows_on_the_partition_column():
    # The partition column and the filter column must be the same one, or the union of day builds
    # stops equalling the full build.
    src = FCT.read_text()
    assert "where {{ adsb_day_in(bd, 's.capture_date') }}" in src, "fct must filter on its partition column"
    assert "and {{ adsb_day_in(bd, 'bf.day_key') }}" in src
    # capture_date alone cannot use bronze's (capture_ts, hex) key and reads every granule; the same-day
    # capture_ts bounds select identical rows and prune the scan, on a contiguous set only.
    assert re.search(
        r"\{%- if bd\.prune_ts %\}.*?"
        r"and s\.capture_ts >= toUnixTimestamp\(toDateTime\('\{\{ bd\.lo \}\}', 'UTC'\)\)\s*\n"
        r"\s*and s\.capture_ts <  toUnixTimestamp\(toDateTime\('\{\{ bd\.hi_excl \}\}', 'UTC'\)\)\s*\n"
        r"\s*\{%- endif %\}",
        src, re.DOTALL,
    ), "fct_adsb_state lost the capture_ts bounds that prune bronze's primary key"
    # capture_date is appended last, so every existing column keeps its position for downstream readers.
    tail = src[src.rindex("as reg_country"):]
    assert re.search(r"as reg_country,\s*\n\s*b\.capture_date\s*\nfrom base b", tail)


def test_callsign_model_reads_only_the_day_set():
    src = CALLSIGN.read_text()
    assert "and {{ adsb_day_in(bd, 'capture_date') }}" in src
    assert re.search(
        r"\{%- if bd\.prune_ts %\}.*?and capture_ts >= toUnixTimestamp\(toDateTime\('\{\{ bd\.lo \}\}'",
        src, re.DOTALL,
    )
    # a gappy set bounds OpenSky per day too, or the ASOF build side grows to the whole span
    assert re.search(
        r"\{%- if not bd\.prune_ts %\}.*?adsb_day_in\(bd, \"toDate\(snapshot_time - \" ~ shift\).*?"
        r"adsb_day_in\(bd, \"toDate\(snapshot_time \+ \" ~ shift\)", src, re.DOTALL
    )
