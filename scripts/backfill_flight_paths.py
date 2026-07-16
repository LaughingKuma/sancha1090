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
from include.db import analytics_engine
from scripts.backfill_adsblol_states import _day_range

# First day with rooftop coverage in bronze.adsb_states (min capture_date, verified 2026-07-11).
ROOFTOP_ERA_START = date(2026, 5, 23)


def _workers_int(value: str) -> int:
    # Politeness over speed against adsb.lol's free static hosting (scope pins workers=2-3);
    # 4 is headroom, an unbounded value (e.g. a stray zero or typo'd 20) would hammer them.
    n = int(value)
    if not 1 <= n <= 4:
        raise argparse.ArgumentTypeError(f"--workers must be 1..4 (got {n})")
    return n


def _progress(day):
    # Block-buffered docker-exec stdout stays silent for a whole day otherwise; heartbeat
    # every 100 fetches (and the final pair) so a long run visibly advances.
    def cb(done, total):
        if done % 100 == 0 or done == total:
            print(f"    {day}: {done}/{total} fetched", flush=True)
    return cb


def _dry_run(start: date, end: date, engine) -> int:
    grand_total = 0
    for day in _day_range(start, end):
        cohort = routes.rooftop_cohort(day)
        pairs = [(h, day.isoformat()) for h in cohort]
        pending = ledger.filter_unattempted(pairs, engine)
        grand_total += len(pending)
        print(f"{day}: {len(cohort)} cohort hexes, {len(pending)} pending", flush=True)
    print(f"TOTAL pending across era: {grand_total}", flush=True)
    return 0


def _live_run(start: date, end: date, engine, workers: int) -> int:
    failures: list[str] = []
    totals = {"fetched": 0, "landed": 0, "missing": 0, "errors": 0}
    started = time.monotonic()
    for day in _day_range(start, end):
        try:
            cohort = routes.rooftop_cohort(day)
            res = routes.run_daily(day, targets=cohort, engine=engine, workers=workers,
                                   progress=_progress(day))
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
    print("Ledger-backed: a rerun resumes for free — already-attempted pairs are skipped, and "
          "errored pairs stay eligible and retry on any rerun after a ~4-minute cooldown — the "
          "ledger never abandons them.", flush=True)
    return 1 if failures else 0


def run(start: date, end: date, workers: int, dry_run: bool) -> int:
    engine = analytics_engine()  # one pool for the whole run, not one per day
    if dry_run:
        return _dry_run(start, end, engine)
    return _live_run(start, end, engine, workers)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill bronze.adsblol_flight_paths/segments for the rooftop cohort era")
    p.add_argument("--start", default=ROOFTOP_ERA_START.isoformat(),
                   help=f"first day, YYYY-MM-DD (default {ROOFTOP_ERA_START.isoformat()}, rooftop era start)")
    p.add_argument("--end", default=None, help="last day inclusive, YYYY-MM-DD (default yesterday UTC)")
    p.add_argument("--workers", type=_workers_int, default=2,
                   help="concurrent fetch workers, 1..4 (default 2)")
    p.add_argument("--dry-run", action="store_true",
                   help="count ledger-pending (day, hex) pairs only, fetch nothing")
    args = p.parse_args()

    end = (date.fromisoformat(args.end) if args.end
           else (datetime.now(timezone.utc) - timedelta(days=1)).date())
    return run(date.fromisoformat(args.start), end, args.workers, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
