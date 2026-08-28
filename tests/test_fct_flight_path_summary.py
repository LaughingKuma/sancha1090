from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODEL = REPO / "dbt" / "sancha1090" / "models" / "marts" / "fct_flight_path_summary.sql"


def _cte(src: str, name: str, next_name: str) -> str:
    # Slice ONE named CTE and strip -- comments, so pins match executable SQL and are stage-specific.
    start = src.index(f"{name} as (")
    end = src.index(f"{next_name} as (", start)
    return re.sub(r"--[^\n]*", "", src[start:end])


def test_partition_guard_fails_before_any_statement():
    # Against the pre-#194 unpartitioned table REPLACE PARTITION fails only after creating __dbt_new_data,
    # leaving one orphan per tick; the guard must read system.tables and raise at compile instead.
    src = MODEL.read_text()
    assert re.search(
        r"is_incremental\(\)\s*%\}.*?run_query\(\"select partition_key from system\.tables",
        src, re.DOTALL,
    ), "fct_flight_path_summary lost the partition_key guard on the incremental branch"
    assert "!= 'day_key'" in src
    assert "run this model once with --full-refresh" in src
    # the guard must raise before any statement: it precedes the first CTE, not just is_incremental()
    assert src.index("run_query(") < src.index("with parent_parts as (")


def test_due_days_compare_parent_part_fingerprint():
    # Nomination state is the summary itself: the parent's newest active part per day vs the stored
    # path_parts_mtime. A failed child run changes nothing, so the same days are due on the retry.
    src = MODEL.read_text()
    parts = _cte(src, "parent_parts", "build_days")
    assert re.search(r"max\(modification_time\)\s+as\s+path_parts_mtime", parts, re.IGNORECASE)
    assert re.search(r"from\s+system\.parts", parts, re.IGNORECASE)
    assert re.search(r"\band\s+active\b", parts)
    due = _cte(src, "build_days", "pts")
    assert re.search(
        r"s\.seen\s+is\s+null\s+or\s+s\.seen\s+!=\s+p\.path_parts_mtime", due, re.IGNORECASE
    ), "build_days must nominate a day when its summary fingerprint is missing OR differs from system.parts"
    assert re.search(r"max\(path_parts_mtime\)\s+as\s+seen\s+from\s+\{\{\s*this\s*\}\}", due, re.IGNORECASE)
    # and the rebuilt rows must persist the fingerprint, or every day stays due forever
    final = src[src.rindex("select"):]
    assert re.search(r"pp\.path_parts_mtime\s+as\s+path_parts_mtime", final), "final projection lost path_parts_mtime"


def test_due_days_skip_days_with_no_current_spine_start():
    # A day with no current spine start summarises to zero rows, REPLACEs nothing and would stay due forever.
    due = _cte(MODEL.read_text(), "build_days", "pts")
    assert re.search(
        r"p\.day\s+in\s*\(\s*select\s+toDate\(start_time\)\s+from\s+\{\{\s*ref\(['\"]fct_flights_reconciled['\"]\)\s*\}\}\s*\)",
        due, re.IGNORECASE,
    ), "build_days is missing the current-spine-start guard"


def test_due_days_newest_first_capped():
    # The cap binds only while a dropped table drains; newest first so the tier seam and share gate recover first.
    due = _cte(MODEL.read_text(), "build_days", "pts")
    assert re.search(
        r"order\s+by\s+p\.day\s+desc\s+limit\s+\{\{\s*var\('path_summary_chunk_days'\)\s*\}\}", due, re.IGNORECASE
    ), "build_days must drain newest-first under path_summary_chunk_days"


def test_full_refresh_lands_every_parent_day_in_one_insert():
    # One partition per parent day: --full-refresh inserts them all in one block, past ClickHouse's default 100.
    src = MODEL.read_text()
    assert "'max_partitions_per_insert_block': 10000" in src, "config lost max_partitions_per_insert_block=10000"
    assert re.search(r"partition_by='day_key'", src)
    assert "incremental_strategy='insert_overwrite'" in src
    due = _cte(src, "build_days", "pts")
    assert re.search(r"flags\.FULL_REFRESH\s*%\}\s*select\s+day\s+from\s+parent_parts\s*\{%", due), (
        "the --full-refresh branch must nominate every parent day (no cap)"
    )
