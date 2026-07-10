from __future__ import annotations

# Re-fetch trace-days the OLD segmenter fused at a missed landing (low boundary fix + turnaround
# gap in one seg_start); ledger rows are cleared first so the 'landed' pairs refetch and re-segment.
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone

from include import adsblol_route_ledger as ledger
from include.adsblol_routes import LOW_FIX_ALT_FT, LOW_FIX_GAP_S, run_daily
from include.clickhouse import (
    ch_client,
    load_adsblol_paths_pending_to_ch,
    load_adsblol_segments_pending_to_ch,
)

# Interpolates the Task 1 constants directly (feet, epoch seconds) — never hardcode copies.
AFFECTED_SQL = f"""
WITH gaps AS (
  SELECT icao24, trace_day, seg_start,
    ts, lagInFrame(ts, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_ts,
    alt_ft, lagInFrame(alt_ft, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_alt_ft
  FROM bronze.adsblol_flight_paths FINAL
)
SELECT DISTINCT icao24, trace_day
FROM gaps
WHERE prev_ts IS NOT NULL
  AND ts - prev_ts >= {LOW_FIX_GAP_S}
  AND least(coalesce(prev_alt_ft, 99999.), coalesce(alt_ft, 99999.)) < {LOW_FIX_ALT_FT}
ORDER BY trace_day, icao24
"""


def affected_pairs(client=None) -> list[tuple[str, str]]:
    from include.clickhouse import ch_client

    c = client or ch_client()
    try:
        rows = c.query(AFFECTED_SQL).result_rows
    finally:
        if client is None:
            c.close()
    # Lower icao24 to match route_targets / ledger keys; trace_day comes back as a CH Date.
    return [(str(r[0]).lower(),
             r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1]))
            for r in rows]


def _progress(day):
    # Block-buffered docker-exec stdout stays silent for a whole day otherwise; heartbeat
    # every 100 fetches (and the final pair) so a long run visibly advances.
    def cb(done, total):
        if done % 100 == 0 or done == total:
            print(f"    {day}: {done}/{total} fetched", flush=True)
    return cb


_SUPERSEDED_TABLES = ("bronze.adsblol_flight_segments", "bronze.adsblol_flight_paths")


def _clear_superseded(client, day: str, hexes: list[str], run_start: str):
    # Both bronze tables are RMT ORDER BY (…, seg_start/ts); a corrected segment that drops a
    # landing's leading ground cluster gets a NEW seg_start, so the RMT never replaces the old fused
    # row. Delete the pre-run rows explicitly (this run's landed hexes, stamped before run_start).
    cleared = None
    for table in _SUPERSEDED_TABLES:
        summary = client.command(
            f"DELETE FROM {table} WHERE trace_day = %(day)s AND icao24 IN %(hexes)s "
            f"AND ingested_at < %(run_start)s",
            parameters={"day": day, "hexes": hexes, "run_start": run_start})
        written = getattr(summary, "written_rows", None)
        if written is not None:
            cleared = (cleared or 0) + int(written)
    return cleared


def run(*, execute: bool = False, sleep: float = 0.2, days_limit=None, workers: int = 5,
        accept_missing: bool = False) -> int:
    # One stamp before any work: the delete lower-bounds on it so freshly re-segmented rows survive.
    run_start = datetime.now(timezone.utc).isoformat()
    by_day: dict[str, list[str]] = defaultdict(list)
    for icao24, day in affected_pairs():
        by_day[day].append(icao24)
    days = sorted(by_day)
    if days_limit is not None:
        days = days[:days_limit]

    total = sum(len(by_day[d]) for d in days)
    print(f"affected: {total} pairs across {len(days)} days (execute={execute})", flush=True)

    if not execute:
        for d in days:
            print(f"  {d}: {len(by_day[d])} pairs", flush=True)
        print("dry-run: nothing deleted or fetched. Re-run with --execute to apply.", flush=True)
        return 0

    error_count = 0
    missing_count = 0
    landed_by_day: dict[str, list[str]] = {}
    for d in days:
        hexes = sorted(set(by_day[d]))
        # Clear only this day's ledger rows; run_daily's D-1 pairs stay 'landed' and are skipped.
        cleared = ledger.delete_attempts([(h, d) for h in hexes])
        res = run_daily(date.fromisoformat(d), targets=hexes, spacing_s=sleep,
                        progress=_progress(d), workers=workers)
        error_count += res["errors"]
        missing_count += res["missing"]
        landed_by_day[d] = res["landed_hexes"]
        print(f"  {d}: pairs={len(hexes)} cleared={cleared} workers={workers} "
              f"fetched={res['fetched']} landed={res['landed']} missing={res['missing']} "
              f"errors={res['errors']} seg_rows={res['rows']} path_rows={res['path_rows']}", flush=True)

    # Drain the freshly-written parquet to CH exactly as the DAG's load_to_clickhouse task does,
    # even if some days errored — the script is restartable, so partial progress must still land.
    segs = load_adsblol_segments_pending_to_ch()
    paths = load_adsblol_paths_pending_to_ch()
    print(f"clickhouse load: segments={segs} paths={paths}", flush=True)
    # Both loaders are best-effort and never raise (see _drain_transformed in include/clickhouse.py),
    # so a failed drain must still flip the exit code — same ok-gate the DAG's load task uses.
    drain_ok = bool(segs.get("ok") and paths.get("ok"))

    # Drain first, delete after: a crash before the delete leaves old+new coexisting, but the old
    # fused row keeps the pair in affected_pairs so a rerun re-lands and deletes — converges. Never
    # delete when the replacement data didn't verifiably land.
    if drain_ok:
        client = ch_client()
        try:
            for d in days:
                hexes = landed_by_day.get(d) or []
                if not hexes:
                    continue
                cleared_old = _clear_superseded(client, d, hexes, run_start)
                if cleared_old is None:
                    print(f"  {d}: cleared superseded rows (count unavailable)", flush=True)
                else:
                    print(f"  {d}: cleared_old={cleared_old}", flush=True)
        finally:
            client.close()

    # Missing traces leave their old rows in place (deletes are landed-only); surface it and, unless
    # explicitly accepted, fail the run so a rerun re-lands them once adsb.lol publishes the trace.
    if missing_count:
        note = " (accepted via --accept-missing)" if accept_missing else " (rerun or pass --accept-missing)"
        print(f"missing: {missing_count} trace(s) not found; old rows retained for those pairs{note}.",
              flush=True)

    return 1 if error_count or (missing_count and not accept_missing) or not drain_ok else 0


def _nonneg_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"--days must be >= 0 (got {n})")
    return n


def _workers_int(value: str) -> int:
    n = int(value)
    if not 1 <= n <= 8:
        raise argparse.ArgumentTypeError(f"--workers must be 1..8 (got {n})")
    return n


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Re-fetch adsb.lol trace-days the old segmenter fused at a missed landing.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--execute", action="store_true",
                   help="Delete ledger rows and refetch. Without it this is a dry run.")
    g.add_argument("--dry-run", action="store_true",
                   help="Default: print per-day pair counts and exit, mutating nothing.")
    p.add_argument("--sleep", type=float, default=0.2,
                   help="Politeness delay (s) between trace fetches (default 0.2).")
    p.add_argument("--days", type=_nonneg_int, default=None,
                   help="Limit to the first N affected days (pilot run).")
    p.add_argument("--workers", type=_workers_int, default=5,
                   help="Concurrent fetch workers, 1..8 (default 5; 1 = serial).")
    p.add_argument("--accept-missing", action="store_true",
                   help="Don't fail the run on missing traces (old rows still retained for them).")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    return run(execute=args.execute, sleep=args.sleep, days_limit=args.days, workers=args.workers,
               accept_missing=args.accept_missing)


if __name__ == "__main__":
    raise SystemExit(main())
