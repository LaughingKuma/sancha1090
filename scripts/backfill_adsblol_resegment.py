from __future__ import annotations

# Re-fetch trace-days the segmenter's new slow-gap arm would now split (turnaround-sized silence,
# below the cruise ceiling, implied cross-gap speed too slow to have stayed airborne).
import argparse
from collections import defaultdict
from datetime import date, datetime, timezone

from include import adsblol_route_ledger as ledger
from include.adsblol_routes import SLOW_GAP_CEIL_FT, SLOW_GAP_S, SLOW_GAP_SPEED_KMH, run_daily
from include.clickhouse import (
    ch_client,
    load_adsblol_paths_pending_to_ch,
    load_adsblol_segments_pending_to_ch,
)

# Interpolates the Task 1 constants directly (feet, epoch seconds, km/h) — never hardcode copies.
# The speed expression mirrors _haversine_km term-for-term (R=6371.0, division form) so a pair is
# SQL-selected iff the Python arm splits it — the dry-run converges to zero. The gap>=SLOW_GAP_S
# conjunct keeps the divisor >= 1800 s, so the float division never blows up (CH won't raise anyway).
AFFECTED_SQL = f"""
WITH gaps AS (
  SELECT icao24, trace_day, seg_start,
    ts, lagInFrame(ts, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_ts,
    alt_ft, lagInFrame(alt_ft, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_alt_ft,
    lat, lagInFrame(lat, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_lat,
    lon, lagInFrame(lon, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_lon
  FROM bronze.adsblol_flight_paths FINAL
)
SELECT DISTINCT icao24, trace_day
FROM gaps
WHERE prev_ts IS NOT NULL
  AND ts - prev_ts >= {SLOW_GAP_S}
  AND least(coalesce(prev_alt_ft, 99999.), coalesce(alt_ft, 99999.)) < {SLOW_GAP_CEIL_FT}
  AND 2 * 6371.0 * asin(sqrt(
        pow(sin(radians(lat - prev_lat) / 2), 2)
        + cos(radians(prev_lat)) * cos(radians(lat)) * pow(sin(radians(lon - prev_lon) / 2), 2)
      )) / ((ts - prev_ts) / 3600.) < {SLOW_GAP_SPEED_KMH}
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

# FINAL is load-bearing: identical-key re-inserts (the RMT collapses them later) must not flag a
# hex-day; only re-keyed leftovers from an interrupted run should.
_STALE_HEXDAYS_SQL = """
SELECT trace_day, icao24, max(ingested_at) AS mx
FROM {table} FINAL
GROUP BY trace_day, icao24
HAVING uniqExact(ingested_at) > 1
ORDER BY trace_day, icao24
"""


def sweep_stale(client=None, *, execute: bool = False):
    # Paths RMT-replace in place (AFFECTED_SQL goes quiet), so a crash before delete leaves
    # re-keyed rows behind forever; batch-mixed hex-days under FINAL are that exact signature.
    c = client or ch_client()
    try:
        found_any = False
        deleted = None
        for table in _SUPERSEDED_TABLES:
            rows = c.query(_STALE_HEXDAYS_SQL.format(table=table)).result_rows
            by_day_mx: dict[tuple[str, object], list[str]] = defaultdict(list)
            for r in rows:
                day = r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0])
                by_day_mx[(day, r[2])].append(r[1])
            for (day, mx), hexes in sorted(by_day_mx.items(), key=lambda kv: kv[0][0]):
                found_any = True
                hexes = sorted(hexes)
                if not execute:
                    print(f"stale sweep (dry-run) {table} {day}: {len(hexes)} hex-days", flush=True)
                    continue
                # mx is this hex-day's newest landed batch, by definition of the signature — only
                # rows strictly older than it are the pre-run leftovers, never the replacement.
                params = {"day": day, "hexes": hexes, "mx": mx}
                # Lightweight DELETE always reports written_rows=0, so count the same predicate
                # up front and use that instead.
                cleared = c.query(
                    f"SELECT count() FROM {table} WHERE trace_day = %(day)s "
                    f"AND icao24 IN %(hexes)s AND ingested_at < %(mx)s",
                    parameters=params).result_rows[0][0]
                c.command(
                    f"DELETE FROM {table} WHERE trace_day = %(day)s AND icao24 IN %(hexes)s "
                    f"AND ingested_at < %(mx)s",
                    parameters=params)
                deleted = (deleted or 0) + int(cleared)
                print(f"stale sweep {table} {day}: {len(hexes)} hex-days cleared_rows={cleared}",
                      flush=True)
        if not found_any:
            print("stale sweep: clean", flush=True)
        return deleted
    finally:
        if client is None:
            c.close()


def _clear_superseded(client, day: str, hexes: list[str], run_start: str):
    # Both bronze tables are RMT ORDER BY (…, seg_start/ts); a corrected segment that drops a
    # landing's leading ground cluster gets a NEW seg_start, so the RMT never replaces the old fused
    # row. Delete the pre-run rows explicitly (this run's landed hexes, stamped before run_start).
    cleared = 0
    for table in _SUPERSEDED_TABLES:
        params = {"day": day, "hexes": hexes, "run_start": run_start}
        # pre-count: lightweight DELETE reports written_rows=0 (see sweep_stale)
        count = client.query(
            f"SELECT count() FROM {table} WHERE trace_day = %(day)s AND icao24 IN %(hexes)s "
            f"AND ingested_at < %(run_start)s",
            parameters=params).result_rows[0][0]
        client.command(
            f"DELETE FROM {table} WHERE trace_day = %(day)s AND icao24 IN %(hexes)s "
            f"AND ingested_at < %(run_start)s",
            parameters=params)
        cleared += int(count)
    return cleared


def run(*, execute: bool = False, sleep: float = 0.2, days_limit=None, workers: int = 5,
        accept_missing: bool = False) -> int:
    # One stamp before any work: the delete lower-bounds on it so freshly re-segmented rows survive.
    run_start = datetime.now(timezone.utc).isoformat()
    # Repair leftovers from an interrupted run first; report-only under --days so a pilot
    # run never deletes beyond its window (explicit --sweep-stale stays global by intent).
    sweep_execute = execute and days_limit is None
    sweep_stale(execute=sweep_execute)
    if execute and not sweep_execute:
        print("stale sweep: report-only under --days; run --sweep-stale to clear.", flush=True)
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

    # Drain first, delete after — never delete when the replacement didn't verifiably land. A crash
    # here isn't re-selected later (paths self-heal in place); the start-of-run sweep converges it.
    if drain_ok:
        client = ch_client()
        try:
            for d in days:
                hexes = landed_by_day.get(d) or []
                if not hexes:
                    continue
                cleared_old = _clear_superseded(client, d, hexes, run_start)
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
    p.add_argument("--sweep-stale", action="store_true",
                   help="Only sweep re-keyed superseded bronze rows left by an interrupted run, "
                        "then exit.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.sweep_stale:
        sweep_stale(execute=args.execute)
        return 0
    return run(execute=args.execute, sleep=args.sleep, days_limit=args.days, workers=args.workers,
               accept_missing=args.accept_missing)


if __name__ == "__main__":
    raise SystemExit(main())
