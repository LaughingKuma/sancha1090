"""Guard the OpenSky daily credit budget.

OpenSky's REST API charges credits per /states/all call based on bounding
box area. We pull a single Japan+ocean bbox (>400 sq deg = 4 credits) at the
current ingest cadence, and must stay under the quota the API actually
meters us at — observed live 2026-06-10 via x-rate-limit-remaining: the
4000 registered tier. Active-feeder 8000 only accrues once the receiver
logs >=30% monthly uptime (status recalculated ~2-hourly); raise the
budget back to 8000 only after the header confirms the promotion.

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
# single Japan box at the current cadence sits far under the 4000 quota
# (~480/day happy path), so this factor is now slack, not a tight constraint —
# it stays as a deliberate margin if the box is split or the cadence raised.
RETRY_BUDGET_FACTOR = 1.04


def credit_cost(bbox_area_sq_deg: float) -> int:
    """Return the OpenSky credit cost for a single /states/all call."""
    for upper, cost in CREDIT_TIERS:
        if bbox_area_sq_deg <= upper:
            return cost


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


# ── Flights bucket (v5.1) ────────────────────────────────────────────────────
# /flights/* draws from a credit bucket INDEPENDENT of /states (verified
# 2026-06-10: four flights calls left the states bucket untouched), so the
# backstory ring can never starve the states feed. Observed: 30 credits/call
# against a ~4000/day bucket.

FLIGHTS_DAILY_CREDIT_BUDGET = 4000
FLIGHTS_CREDITS_PER_CALL = 30
# Mirrors the windows in dags/ingest_flights.py: arrivals D-2, departures D-2,
# departures D-0. Update both together.
FLIGHTS_CALLS_PER_AIRPORT_PER_DAY = 3


def test_flights_daily_credit_consumption_under_budget():
    from include.airports_jp import AIRPORTS_JP

    daily = len(AIRPORTS_JP) * FLIGHTS_CALLS_PER_AIRPORT_PER_DAY * FLIGHTS_CREDITS_PER_CALL
    daily_with_retries = int(daily * RETRY_BUDGET_FACTOR)

    assert daily_with_retries <= FLIGHTS_DAILY_CREDIT_BUDGET, (
        f"Daily flights-bucket consumption ({daily_with_retries}, including retry buffer) "
        f"exceeds the flights budget ({FLIGHTS_DAILY_CREDIT_BUDGET}).\n"
        f"  Fixes: drop airports from include/airports_jp.py or cut a window."
    )


def test_flights_airports_are_valid_jp_icao():
    from include.airports_jp import AIRPORTS_JP

    icaos = [a["icao"] for a in AIRPORTS_JP]
    assert len(icaos) == len(set(icaos)), "duplicate airports in AIRPORTS_JP"
    for icao in icaos:
        # RJ** = mainland Japan, RO** = Okinawa region
        assert len(icao) == 4 and icao[:2] in ("RJ", "RO"), f"not a JP ICAO code: {icao}"
