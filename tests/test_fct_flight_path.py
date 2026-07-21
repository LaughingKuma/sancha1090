from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODEL = REPO / "dbt" / "sancha1090" / "models" / "marts" / "fct_flight_path.sql"


def _cte(src: str, name: str, next_name: str) -> str:
    # Slice ONE named CTE and strip -- comments, so pins match executable SQL and are stage-specific.
    start = src.index(f"{name} as (")
    end = src.index(f"{next_name} as (", start)
    return re.sub(r"--[^\n]*", "", src[start:end])


def test_stale_days_skips_days_with_no_current_spine_start():
    # Without the guard a no-current-start day re-nominates forever (zero-row rebuild never REPLACEs the
    # partition, stalling the watermark); day-grain placement keeps it from blocking the batch behind it.
    cte = _cte(MODEL.read_text(), "stale_days", "orphan_days")
    guard = re.compile(
        r"where\s+p\.day_key\s+in\s*\(\s*select\s+toDate\(start_time\)\s+from\s+"
        r"\{\{\s*ref\(['\"]fct_flights_reconciled['\"]\)\s*\}\}\s*\)",
        re.IGNORECASE,
    )
    assert guard.search(cte), (
        "fct_flight_path stale_days is missing the current-spine-start guard "
        "(where p.day_key in (select toDate(start_time) from fct_flights_reconciled)); "
        "without it an unfixable orphan day re-nominates forever and stalls the watermark"
    )


def test_stale_days_nominates_distinct_day_grain():
    # Stage 1 must be DISTINCT day_keys: duplicate path-row grain must never consume the batch cap.
    cte = _cte(MODEL.read_text(), "stale_days", "orphan_days")
    assert re.search(r"select\s+distinct\s+p\.day_key", cte, re.IGNORECASE), (
        "fct_flight_path stale_days must nominate DISTINCT day_keys (day grain) so duplicate "
        "path rows never consume the orphan batch cap"
    )


def test_orphan_days_takes_oldest_contiguous_run_capped():
    # Stage 2: oldest contiguous ASCENDING run only (non-contiguous days widen chunk_bounds across
    # intervening history toward the 16 GB backstop), capped at the chunk size.
    cte = _cte(MODEL.read_text(), "orphan_days", "forward_days")
    assert re.search(r"row_number\(\)\s+over\s*\(\s*order\s+by\s+day\s*\)", cte, re.IGNORECASE), (
        "orphan_days must row_number over ascending day -- descending order empties the contiguity predicate"
    )
    assert re.search(r"min\(day\)\s+over\s*\(\)", cte, re.IGNORECASE), (
        "orphan_days must take min(day) as a window beside row_number (single stale_days evaluation)"
    )
    assert re.search(r"dateDiff\('day',\s*day0,\s*day\)\s*=\s*rn\s*-\s*1", cte, re.IGNORECASE), (
        "fct_flight_path orphan_days lost the oldest-contiguous-run predicate (dateDiff vs row_number)"
    )
    assert re.search(
        r"order\s+by\s+day\s+limit\s+\{\{\s*var\('path_build_chunk_days'\)\s*\}\}", cte, re.IGNORECASE
    ), "orphan_days must cap the ascending contiguous batch at path_build_chunk_days"


def test_forward_days_suppressed_while_orphans_pending():
    # Orphan nomination stays exclusive of forward days AND must be a conjunction: an OR would mix forward
    # days into an orphan batch and widen chunk_bounds across history.
    src = MODEL.read_text()
    fwd_start = src.index("forward_days as (")
    fwd = re.sub(r"--[^\n]*", "", src[fwd_start:src.index("chunk_days as (", fwd_start)])
    assert re.search(
        r"not\s+exists\s*\(\s*select\s+1\s+from\s+orphan_days\s*\)\s+and\b", fwd, re.IGNORECASE
    ), "forward_days must stay suppressed (not exists orphan_days) AND-conjoined with its other predicates"


def test_unfixable_orphan_operator_runbook_documented():
    # The guard trades a stall for a red FK test; the manual remedy must stay in the operator header.
    src = MODEL.read_text()
    assert re.search(
        r"ALTER\s+TABLE\s+gold_ch\.fct_flight_path\s+DROP\s+PARTITION", src, re.IGNORECASE
    ), "fct_flight_path header no longer documents the manual DROP PARTITION remedy for an unfixable orphan"
