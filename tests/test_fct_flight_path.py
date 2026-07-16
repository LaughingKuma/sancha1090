from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODEL = REPO / "dbt" / "sancha1090" / "models" / "marts" / "fct_flight_path.sql"


def _orphan_day_cte(src: str) -> str:
    # orphan_day is a named CTE; slice from its header to the next CTE (forward_days).
    start = src.index("orphan_day as (")
    end = src.index("forward_days as (", start)
    return src[start:end]


def test_orphan_day_skips_days_with_no_current_spine_start():
    # Drop the guard and orphan_day re-nominates a day with no current-spine start forever (its rebuild
    # stages zero rows, so insert_overwrite never REPLACEs the partition) -- the watermark stalls silently.
    # Pin the SQL text the way test_regions_sync.py does, since the live warehouse hides the regression.
    cte = _orphan_day_cte(MODEL.read_text())
    guard = re.compile(
        r"where\s+p\.day_key\s+in\s*\(\s*select\s+toDate\(start_time\)\s+from\s+"
        r"\{\{\s*ref\(['\"]fct_flights_reconciled['\"]\)\s*\}\}\s*\)",
        re.IGNORECASE,
    )
    assert guard.search(cte), (
        "fct_flight_path orphan_day is missing the current-spine-start guard "
        "(where p.day_key in (select toDate(start_time) from fct_flights_reconciled)); "
        "without it an unfixable orphan day re-nominates forever and stalls the watermark"
    )


def test_unfixable_orphan_operator_runbook_documented():
    # The guard trades a stall for a red FK test; the manual remedy must stay in the operator header.
    src = MODEL.read_text()
    assert re.search(
        r"ALTER\s+TABLE\s+gold_ch\.fct_flight_path\s+DROP\s+PARTITION", src, re.IGNORECASE
    ), "fct_flight_path header no longer documents the manual DROP PARTITION remedy for an unfixable orphan"
