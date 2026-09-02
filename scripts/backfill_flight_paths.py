from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone

# Runs inside an airflow container (docker exec sancha1090-airflow-scheduler-1 ...)
# where Garage/manifest endpoints resolve; scripts/ is bind-mounted there.
sys.path.insert(0, "/opt/airflow")

from include import adsblol_route_ledger as ledger
from include import adsblol_routes as routes
from include.adsblol_release import land_release_day, member_progress
from include.db import analytics_engine
from scripts.backfill_adsblol_states import _day_range

# First day with rooftop coverage in bronze.adsb_states (min capture_date, verified 2026-07-11).
ROOFTOP_ERA_START = date(2026, 5, 23)


def _cohort_targets(day: date, cohort: str) -> list[str]:
    return routes.release_targets(day) if cohort == "reconciled" else routes.rooftop_cohort(day)


def _dry_run(start: date, end: date, engine, cohort: str) -> int:
    grand_total = 0
    for day in _day_range(start, end):
        targets = _cohort_targets(day, cohort)
        pairs = [(h, day.isoformat()) for h in targets]
        pending = ledger.filter_unattempted(pairs, engine)
        grand_total += len(pending)
        label = "reconciled" if cohort == "reconciled" else "cohort"
        print(f"{day}: {len(targets)} {label} hexes, {len(pending)} pending", flush=True)
    print(f"TOTAL pending across era: {grand_total}", flush=True)
    return 0


def _live_run(start: date, end: date, engine, cohort: str) -> int:
    failures: list[str] = []
    totals = {"fetched": 0, "landed": 0, "missing": 0, "errors": 0}
    started = time.monotonic()
    for day in _day_range(start, end):
        try:
            # The prior-day halo is covered by day-1's own extraction set (it already includes this
            # day's hexes), so a wave that wants the halo for its first day starts one day earlier.
            res = land_release_day(day, targets=_cohort_targets(day, cohort), engine=engine,
                                   progress=member_progress(day, "    "))
        except Exception as exc:  # noqa: BLE001 — one bad day must not abort the wave; reruns retry it
            failures.append(f"{day} ({exc})")
            print(f"{day}: FAILED — {exc}", flush=True)
            continue
        for k in totals:
            totals[k] += res[k]
        if res["errors"] > 0:
            # A day can raise nothing yet still leave error pairs behind; name it so the operator
            # sees it in the failure list too, not just buried in a per-day errors= count.
            failures.append(f"{day} (partial: {res['errors']} pair(s) errored)")
        elapsed = time.monotonic() - started
        rate = totals["fetched"] / elapsed if elapsed > 0 else 0.0
        print(f"{day}: landed={res['landed']} missing={res['missing']} errors={res['errors']} "
              f"| totals: fetched={totals['fetched']} landed={totals['landed']} "
              f"missing={totals['missing']} errors={totals['errors']} | {rate:.2f} pairs/s", flush=True)
    if failures:
        print(f"{len(failures)} day(s) failed:", flush=True)
        for f in failures:
            print(f"  {f}", flush=True)
    print("Ledger-backed: a rerun resumes for free — errored and missing pairs re-propose on any "
          "rerun; landed pairs never do.", flush=True)
    return 1 if failures else 0


def run(start: date, end: date, dry_run: bool, cohort: str = "rooftop") -> int:
    engine = analytics_engine()  # one pool for the whole run, not one per day
    if dry_run:
        return _dry_run(start, end, engine, cohort)
    return _live_run(start, end, engine, cohort)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill bronze.adsblol_flight_paths/segments for the rooftop or reconciled cohort")
    p.add_argument("--start", default=None,
                   help=f"first day, YYYY-MM-DD (rooftop defaults to {ROOFTOP_ERA_START.isoformat()}; "
                        "reconciled has no default and must be given explicitly)")
    p.add_argument("--end", default=None, help="last day inclusive, YYYY-MM-DD (default yesterday UTC)")
    p.add_argument("--dry-run", action="store_true",
                   help="count ledger-pending (day, hex) pairs only, extract nothing")
    p.add_argument("--cohort", choices=("rooftop", "reconciled"), default="rooftop",
                   help="rooftop = hexes the roof heard (own-day); reconciled = release_targets "
                        "(our own state hexes on the day and the next)")
    args = p.parse_args()

    if args.start is None:
        if args.cohort == "reconciled":
            p.error("--cohort reconciled requires an explicit --start (rooftop-era default "
                     "would sweep ~8 weeks of release tarballs)")
        start = ROOFTOP_ERA_START
    else:
        start = date.fromisoformat(args.start)

    end = (date.fromisoformat(args.end) if args.end
           else (datetime.now(timezone.utc) - timedelta(days=1)).date())
    return run(start, end, args.dry_run, args.cohort)


if __name__ == "__main__":
    raise SystemExit(main())
