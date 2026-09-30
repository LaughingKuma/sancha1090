from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DBT = REPO / "dbt" / "sancha1090"
MODEL = DBT / "models" / "marts" / "agg_rooftop_airframe_hour.sql"
YML = DBT / "models" / "marts" / "_rooftop.yml"
FIXES_TEST = DBT / "tests" / "assert_rooftop_rollup_fixes_match_bronze.sql"
MACROS = DBT / "macros" / "adsb_rebuild_window.sql"

# X1 and the rooftop stats queries read exactly these; a position column here would ship coordinates.
COLUMNS = [
    "hour_utc", "capture_date", "hex", "typecode", "n_fixes", "callsigns", "flag_ladd", "flag_mil",
    "flag_heli", "minutes", "alt_rec_ft", "gs_rec_kt", "dst_rec_nmi",
]


def _final_select(src: str) -> str:
    return src[src.rindex("\nselect\n"):]


def test_rollup_is_a_gold_adsb_incremental_by_day():
    src = MODEL.read_text()
    assert "materialized='incremental'" in src
    assert "incremental_strategy='insert_overwrite'" in src
    assert "partition_by='capture_date'" in src
    # tag adsb: transform_adsb_silver builds it, transform_marts excludes it; marts/ lands it in gold_ch
    assert "tags=['adsb']" in src
    project = (DBT / "dbt_project.yml").read_text()
    assert "marts:\n      +materialized: table\n      +schema: gold_ch" in project
    # the first build is not a --full-refresh, so the cap cannot key on the flag
    assert "'max_memory_usage': 12000000000" in src and "FULL_REFRESH" not in src
    assert "'max_partitions_per_insert_block': 10000" in src


def test_rollup_windows_like_the_callsign_model_healing_against_itself():
    src = MODEL.read_text()
    assert "{%- set w = adsb_rebuild_window() %}" in src
    assert "{%- if w.windowed %}{% set bd = adsb_build_days(w, count_against='self') %}{% endif %}" in src
    assert "ref('fct_adsb_state')" not in src and "callsign_backfill_rebuild_to" not in src
    sql_start = re.search(r"^with\s*$", src, re.M).start()
    assert src.index("adsb_partition_key_guard('capture_date')") < sql_start
    # partition column == filter column, so the union of day builds equals the full build
    assert "and {{ adsb_day_in(bd, 'capture_date') }}" in src
    assert re.search(
        r"\{%- if bd\.prune_ts %\}.*?"
        r"and capture_ts >= toUnixTimestamp\(toDateTime\('\{\{ bd\.lo \}\}', 'UTC'\)\)\s*\n"
        r"\s*and capture_ts <  toUnixTimestamp\(toDateTime\('\{\{ bd\.hi_excl \}\}', 'UTC'\)\)",
        src, re.DOTALL,
    )


def test_self_count_compares_the_rollups_own_fix_sum():
    src = MACROS.read_text()
    assert "{% macro adsb_build_days(w, count_against='fct') %}" in src
    assert "{% macro adsb_count_mismatch_days(window_days, against='fct') %}" in src
    m = src[src.index("macro adsb_count_mismatch_days"):src.index("macro adsb_day_in")]
    # the rollup's own table, so no dbt edge to fct is needed; absent (first build) means no extra days
    assert "{%- set built = load_relation(this) %}" in m
    assert "{%- if built is none %}{% do return([]) %}{% endif %}" in m
    self_sql = m[m.index("{%- if against == 'self' %}\n    {#-"):m.index("{%- else %}\n    select toString")]
    # the model's row predicate and fix count, or every day reads as mismatched
    assert re.search(
        r"select toString\(capture_date\) as d, count\(\) as n, 'bronze' as src\s*\n"
        r"\s*from \{\{ source\('bronze', 'adsb_states'\) \}\}\s*\n"
        r"\s*where hex is not null and capture_ts is not null group by capture_date", self_sql
    )
    assert (
        "select toString(capture_date), sum(n_fixes), 'built' from {{ built }} group by capture_date"
        in self_sql
    )
    assert "final" not in self_sql.lower()


def test_rollup_carries_the_ladd_and_record_inputs_and_no_position():
    src = MODEL.read_text()
    assert "max(bitAnd(db_flags, 8) != 0) as flag_ladd" in src
    assert "max(bitAnd(db_flags, 1) != 0) as flag_mil" in src
    assert "max(ifNull(category, '') = 'A7') as flag_heli" in src
    assert "groupUniqArrayIf(upper(trimBoth(flight)), trimBoth(ifNull(flight, '')) != '') as callsigns" in src
    assert "assumeNotNull(lower(trimBoth(hex))) as hex" in src
    body = _final_select(src).split("\nfrom per_minute")[0]
    aliased = [c for c in COLUMNS if c not in ("capture_date", "hex")]
    assert re.findall(r"\bas (\w+),?$", body, re.M) == aliased
    assert "\n    capture_date,\n    hex,\n" in body
    assert "r_dir" not in src
    # records: >= 5 fixes per minute, speed cross-checked against position, range within the radio horizon
    assert src.count("n >= 5") == 3
    assert "<= 0.1 * gs_med" in src and "p1.3 - p0.3 >= 20" in src
    assert "dst_med <= 1.23 * (sqrt(alt_med) + sqrt(1000))" in src


def test_rollup_tests_pin_keys_grain_and_fix_count():
    doc = yaml.safe_load(YML.read_text())
    model = next(m for m in doc["models"] if m["name"] == "agg_rooftop_airframe_hour")
    assert [c["name"] for c in model["columns"]] == COLUMNS
    not_null = {c["name"] for c in model["columns"] if "not_null" in (c.get("tests") or [])}
    assert not_null == {"hour_utc", "capture_date", "hex"}
    assert {"grain": {"arguments": {"column_names": ["hour_utc", "hex"]}}} in model["tests"]
    fixes = FIXES_TEST.read_text()
    assert "config(tags=['adsb'])" in fixes
    # same row predicate as the model, or the sums drift by the rows it drops
    assert "where hex is not null and capture_ts is not null" in fixes
    # every day, not just the window: a day healed or deferred outside it must still show here
    assert "sum(n_fixes), 'rollup'" in fixes and "union all" in fixes
    assert "having bronze_rows != rollup_rows" in fixes
    assert "callsign_backfill_rebuild" not in fixes and "between" not in fixes
