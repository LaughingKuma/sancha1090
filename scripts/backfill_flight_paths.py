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


def _dry_run(start: date, end: date, engine, cohort: str) -> int:
    grand_total = 0
    if cohort == "reconciled":
        # Live runs record attempts as they go; the dry-run must not double-count the halo
        # overlap between adjacent days.
        proposed: set[tuple[str, str]] = set()
        for day in _day_range(start, end):
            hexes = routes.route_targets(day)
            halo = [(h, d) for h in hexes
                    for d in (day.isoformat(), (day - timedelta(days=1)).isoformat())]
            pairs = [p for p in halo if p not in proposed]
            proposed.update(pairs)
            pending = ledger.filter_unattempted(pairs, engine)
            grand_total += len(pending)
            print(f"{day}: {len(hexes)} reconciled hexes, {len(pending)} pending", flush=True)
        print(f"TOTAL pending across era: {grand_total}", flush=True)
        return 0
    for day in _day_range(start, end):
        rooftop = routes.rooftop_cohort(day)
        pairs = [(h, day.isoformat()) for h in rooftop]
        pending = ledger.filter_unattempted(pairs, engine)
        grand_total += len(pending)
        print(f"{day}: {len(rooftop)} cohort hexes, {len(pending)} pending", flush=True)
    print(f"TOTAL pending across era: {grand_total}", flush=True)
    return 0


def _live_run(start: date, end: date, engine, workers: int, cohort: str) -> int:
    failures: list[str] = []
    totals = {"fetched": 0, "landed": 0, "missing": 0, "errors": 0}
    started = time.monotonic()
    for day in _day_range(start, end):
        try:
            if cohort == "reconciled":
                # No-targets run_daily keeps the (day, day-1) halo: pre-start pad fixes for a
                # 09:0x JST departure live in the prior UTC day's trace.
                res = routes.run_daily(day, engine=engine, workers=workers, progress=_progress(day))
            else:
                rooftop = routes.rooftop_cohort(day)
                res = routes.run_daily(day, targets=rooftop, engine=engine, workers=workers,
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


def run(start: date, end: date, workers: int, dry_run: bool, cohort: str = "rooftop") -> int:
    engine = analytics_engine()  # one pool for the whole run, not one per day
    if dry_run:
        return _dry_run(start, end, engine, cohort)
    return _live_run(start, end, engine, workers, cohort)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backfill bronze.adsblol_flight_paths/segments for the rooftop or reconciled cohort")
    p.add_argument("--start", default=None,
                   help=f"first day, YYYY-MM-DD (rooftop defaults to {ROOFTOP_ERA_START.isoformat()}; "
                        "reconciled has no default and must be given explicitly)")
    p.add_argument("--end", default=None, help="last day inclusive, YYYY-MM-DD (default yesterday UTC)")
    p.add_argument("--workers", type=_workers_int, default=2,
                   help="concurrent fetch workers, 1..4 (default 2)")
    p.add_argument("--dry-run", action="store_true",
                   help="count ledger-pending (day, hex) pairs only, fetch nothing")
    p.add_argument("--cohort", choices=("rooftop", "reconciled"), default="rooftop",
                   help="rooftop = hexes the roof heard (own-day); reconciled = every reconciled "
                        "flight via no-targets run_daily (fetches day and day-1)")
    args = p.parse_args()

    if args.start is None:
        if args.cohort == "reconciled":
            p.error("--cohort reconciled requires an explicit --start (rooftop-era default "
                     "would sweep ~8 weeks and burn aged-404 retries)")
        start = ROOFTOP_ERA_START
    else:
        start = date.fromisoformat(args.start)

    end = (date.fromisoformat(args.end) if args.end
           else (datetime.now(timezone.utc) - timedelta(days=1)).date())
    return run(start, end, args.workers, args.dry_run, args.cohort)


if __name__ == "__main__":
    raise SystemExit(main())
