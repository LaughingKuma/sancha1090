"""Guard the OpenSky 4000-credit/day daily budget.

OpenSky's REST API charges credits per /states/all call based on bounding
box area. With 8 large continental bboxes (all >400 sq deg = 4 credits
each) and the current ingest cadence, total daily credit consumption must
stay under the 4000-credit registered-user quota.

If this test fails: slow the schedule, drop regions, or shrink the bboxes.
Note that splitting a region into smaller bboxes does NOT save credits — the
per-call discount is smaller than the multiplier from extra calls.
"""

from __future__ import annotations

from include.regions import REGIONS


DAILY_CREDIT_BUDGET = 4000

# OpenSky's tiered cost for /states/all with a bbox, by area in square degrees.
CREDIT_TIERS = [
    (25,           1),   # area ≤ 25
    (100,          2),   # 25 < area ≤ 100
    (400,          3),   # 100 < area ≤ 400
    (float("inf"), 4),   # > 400
]

# Mirrors the cron in dags/ingest_states.py. Update both together — the
# point of this test is to make that update conscious.
INGEST_SCHEDULE = "*/12 * * * *"
RUNS_PER_DAY = 24 * (60 // 12)  # = 120

# Retry budget: every fetch_region task can retry up to 3 times, and each
# attempt's OpenSkyClient retries internally up to 5 times on 429/5xx. The
# happy-path math leaves only ~4% headroom under the 4000-credit ceiling, so a
# burst of 5xx from OpenSky can blow the budget. This factor models a small
# steady retry rate (~4% extra calls). If real-world retry rate exceeds this,
# the schedule needs to slow down.
RETRY_BUDGET_FACTOR = 1.04


def credit_cost(bbox_area_sq_deg: float) -> int:
    """Return the OpenSky credit cost for a single /states/all call."""
    for upper, cost in CREDIT_TIERS:
        if bbox_area_sq_deg <= upper:
            return cost
    return 4


def bbox_area_sq_deg(region: dict) -> float:
    return (region["lamax"] - region["lamin"]) * (region["lomax"] - region["lomin"])


def test_daily_credit_consumption_under_budget():
    per_run = sum(credit_cost(bbox_area_sq_deg(r)) for r in REGIONS)
    daily = per_run * RUNS_PER_DAY
    daily_with_retries = int(daily * RETRY_BUDGET_FACTOR)

    assert daily_with_retries <= DAILY_CREDIT_BUDGET, (
        f"Daily OpenSky credit consumption ({daily_with_retries}, including "
        f"{int((RETRY_BUDGET_FACTOR - 1) * 100)}% retry buffer) exceeds budget "
        f"({DAILY_CREDIT_BUDGET}).\n"
        f"  Happy-path daily: {daily} credits\n"
        f"  Per-run cost: {per_run} credits across {len(REGIONS)} regions\n"
        f"  Runs per day: {RUNS_PER_DAY} (from schedule {INGEST_SCHEDULE})\n"
        f"  Fixes: slow the schedule, drop regions, or shrink bboxes."
    )


def test_all_regions_have_valid_bboxes():
    """Sanity-check that every region's bbox is geographically valid."""
    for region in REGIONS:
        name = region["name"]
        assert region["lamin"] < region["lamax"], f"{name}: lamin >= lamax"
        assert region["lomin"] < region["lomax"], f"{name}: lomin >= lomax"
        assert -90 <= region["lamin"] <= region["lamax"] <= 90, (
            f"{name}: latitude out of range"
        )
        assert -180 <= region["lomin"] <= region["lomax"] <= 180, (
            f"{name}: longitude out of range"
        )
