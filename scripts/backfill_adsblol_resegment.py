from __future__ import annotations

# Re-extract trace-days the segmenter now reads differently: the slow-gap arm, the dwell read and the
# takeoff trim (#229).
# Also the shared re-land wave engine (run/cli/sweep_stale): backfill_adsblol_class1.py drives it with
# its own selector, so this script outlives its own wave's convergence.
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from include import adsblol_route_ledger as ledger
from include.adsblol_release import land_release_day, member_progress
from include.adsblol_routes import (
    DWELL_GS_KT,
    DWELL_S,
    LOW_FIX_ALT_FT,
    SLOW_GAP_CEIL_FT,
    SLOW_GAP_S,
    SLOW_GAP_SPEED_KMH,
)
from include.clickhouse import (
    ch_client,
    load_adsblol_paths_pending_to_ch,
    load_adsblol_segments_pending_to_ch,
)

PATHS_TABLE = "bronze.adsblol_flight_paths"

# Interpolates the Task 1 constants directly (feet, epoch seconds, km/h) — never hardcode copies.
# The speed expression mirrors _haversine_km term-for-term (R=6371.0, division form) so a pair is
# SQL-selected iff the Python arm splits it — the dry-run converges to zero. The gap>=SLOW_GAP_S
# conjunct keeps the divisor >= 1800 s, so the float division never blows up (CH won't raise anyway).
_SLOW_GAP_SQL = f"""
WITH gaps AS (
  SELECT icao24, trace_day, seg_start,
    ts, lagInFrame(ts, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_ts,
    alt_ft, lagInFrame(alt_ft, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_alt_ft,
    lat, lagInFrame(lat, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_lat,
    lon, lagInFrame(lon, 1) OVER (PARTITION BY icao24, trace_day, seg_start ORDER BY ts) AS prev_lon
  FROM {{table}} FINAL
  WHERE trace_day BETWEEN %(lo)s AND %(hi)s
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

# The run finder both _parse_trace mirrors share: maximal {pred} runs on the persisted integer grid, whole-day
# windows (a run spans an old air->ground break), cut at a SLOW_GAP_S silence, kept when spanning >= DWELL_S.
_RUN_SQL = f"""
WITH fixes AS (
  SELECT icao24, trace_day, ts, coalesce(on_ground, false) AS gnd,
    {{pred}} AS in_run,
    lagInFrame(toNullable(in_run), 1, NULL)
      OVER (PARTITION BY icao24, trace_day ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS prev_in_run,
    lagInFrame(ts, 1)
      OVER (PARTITION BY icao24, trace_day ORDER BY ts ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS prev_ts{{extra_select}}
  FROM {{table}} FINAL
  WHERE trace_day BETWEEN %(lo)s AND %(hi)s
),
runs AS (
  SELECT *, sum(if(in_run AND (NOT coalesce(prev_in_run, false)
                               OR toUnixTimestamp(ts) - toUnixTimestamp(prev_ts) >= {SLOW_GAP_S}), 1, 0))
    OVER (PARTITION BY icao24, trace_day ORDER BY ts
          ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS run_id
  FROM fixes
)
SELECT DISTINCT icao24, trace_day
FROM (
  SELECT icao24, trace_day
  FROM runs
  WHERE in_run
  GROUP BY icao24, trace_day, run_id
  HAVING toUnixTimestamp(max(ts)) - toUnixTimestamp(min(ts)) >= {DWELL_S}
     AND {{having}}
)
ORDER BY trace_day, icao24
"""


def _run_sql(pred: str, having: str, extra_select: str = "") -> str:
    # {table} stays a slot: the end-to-end fixture points the statement at a scratch table later.
    return _RUN_SQL.format(pred=pred, having=having, extra_select=extra_select, table="{table}")


# Dwell read: a run still holding an unflagged fix is a hex-day the new walk changes.
_DWELL_PRED = f"coalesce(alt_ft < {LOW_FIX_ALT_FT} AND gs_kt < {DWELL_GS_KT}, false)"
_DWELL_SQL = _run_sql(_DWELL_PRED, "countIf(NOT gnd) > 0")

# Takeoff trim: a ground run on the flag alone with any row after it (NULL at trace end = no trim): the walk never
# writes a trimmed run's roll, so one followed by a ground row after a silence (899000 06-04) is stale too.
_TAKEOFF_PRED = "gnd"
_TAKEOFF_SQL = _run_sql(
    _TAKEOFF_PRED, "argMax(tuple(next_gnd), ts).1 IS NOT NULL",
    extra_select=""",
    leadInFrame(toNullable(gnd), 1, NULL)
      OVER (PARTITION BY icao24, trace_day ORDER BY ts ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING) AS next_gnd""")

# Run one after the other, a week of trace_days at a time: the window over a whole FINAL read buffers every
# partition (one pass hit the 16 GB query cap at 460M fixes), and no run or gap crosses a trace_day.
AFFECTED_SQL_TEMPLATES = (_SLOW_GAP_SQL, _DWELL_SQL, _TAKEOFF_SQL)
SLICE_DAYS = 7


def affected_sqls(*, table: str = PATHS_TABLE) -> tuple[str, ...]:
    return tuple(t.format(table=table) for t in AFFECTED_SQL_TEMPLATES)


def _day_iso(value) -> str:
    # CH returns trace_day as a Date; the ledger stores it as ISO TEXT.
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def affected_pairs(client=None, *, table: str = PATHS_TABLE) -> list[tuple[str, str]]:
    c = client or ch_client()
    pairs: set[tuple[str, str]] = set()
    try:
        # minOrNull: plain min/max over an empty table return 1970-01-01, not NULL, on CH 26.5.
        lo, hi = c.query(
            f"SELECT minOrNull(trace_day), maxOrNull(trace_day) FROM {table}").result_rows[0]
        if lo is None:
            return []
        windows = []
        cursor = lo
        while cursor <= hi:
            windows.append({"lo": cursor.isoformat(),
                            "hi": (cursor + timedelta(days=SLICE_DAYS - 1)).isoformat()})
            cursor += timedelta(days=SLICE_DAYS)
        for sql in affected_sqls(table=table):
            for window in windows:
                # Lower icao24 to match route_targets / ledger keys.
                pairs.update((str(r[0]).lower(), _day_iso(r[1]))
                             for r in c.query(sql, parameters=window).result_rows)
    finally:
        if client is None:
            c.close()
    return sorted(pairs, key=lambda p: (p[1], p[0]))


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
    # Paths RMT-replace in place (the selectors go quiet), so a crash before delete leaves
    # re-keyed rows behind forever; batch-mixed hex-days under FINAL are that exact signature.
    c = client or ch_client()
    try:
        found_any = False
        deleted = None
        for table in _SUPERSEDED_TABLES:
            rows = c.query(_STALE_HEXDAYS_SQL.format(table=table)).result_rows
            by_day_mx: dict[tuple[str, object], list[str]] = defaultdict(list)
            for r in rows:
                by_day_mx[(_day_iso(r[0]), r[2])].append(r[1])
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


def run(*, execute: bool = False, days_limit=None, accept_missing: bool = False,
        affected=None) -> int:
    # One stamp before any work: the delete lower-bounds on it so freshly re-segmented rows survive.
    # Naive-UTC format: CH 26.5 refuses the offset ISO form ('...T...+00:00') for DateTime64 binds.
    run_start = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    # Repair leftovers from an interrupted run first — BEFORE selecting: `affected` is a callable, not a
    # list, because re-keyed leftovers survive FINAL and fake candidates if selection runs un-swept.
    # Report-only under --days so a pilot never deletes beyond its window; a dry run skips the two big
    # scans entirely (its selection sees the same un-swept bronze either way).
    if execute:
        sweep_execute = days_limit is None
        sweep_stale(execute=sweep_execute)
        if not sweep_execute:
            print("stale sweep: report-only under --days; run --sweep-stale to clear.", flush=True)
    by_day: dict[str, list[str]] = defaultdict(list)
    # `or` fallback reads the module global late so a monkeypatched affected_pairs still wins.
    for icao24, day in (affected or affected_pairs)():
        by_day[day].append(icao24)
    days = sorted(by_day)
    if days_limit is not None:
        days = days[:days_limit]

    total = sum(len(by_day[d]) for d in days)
    print(f"affected: {total} pairs across {len(days)} days (execute={execute})", flush=True)

    if not execute:
        for d in days:
            print(f"  {d}: {len(by_day[d])} pairs", flush=True)
        print("dry-run: nothing deleted or extracted. Re-run with --execute to apply.", flush=True)
        return 0

    error_count = 0
    missing_count = 0
    failures: list[str] = []
    landed_by_day: dict[str, list[str]] = {}
    for d in days:
        hexes = sorted(set(by_day[d]))
        try:
            # Clear only this day's ledger rows so the tar re-extracts exactly these hexes.
            cleared = ledger.delete_attempts([(h, d) for h in hexes])
            res = land_release_day(date.fromisoformat(d), targets=set(hexes),
                                   progress=member_progress(d, "    "))
        except Exception as exc:  # noqa: BLE001 — one bad day must not abort the wave
            failures.append(f"{d} ({exc})")
            print(f"  {d}: FAILED — {exc}", flush=True)
            continue
        error_count += res["errors"]
        missing_count += res["missing"]
        landed_by_day[d] = res["landed_hexes"]
        print(f"  {d}: pairs={len(hexes)} cleared={cleared} "
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

    if failures:
        print(f"{len(failures)} day(s) failed: {'; '.join(failures)}", flush=True)
    # Missing traces leave their old rows in place (deletes are landed-only); surface it and, unless
    # explicitly accepted, fail the run so the operator decides whether another variant is worth it.
    if missing_count:
        note = " (accepted via --accept-missing)" if accept_missing else " (pass --accept-missing)"
        print(f"missing: {missing_count} trace(s) not in the release; old rows retained for those "
              f"pairs. Absence in the release is final — a rerun re-proposes them only if another "
              f"variant is landed{note}.", flush=True)

    return 1 if error_count or failures or (missing_count and not accept_missing) or not drain_ok else 0


def _nonneg_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"--days must be >= 0 (got {n})")
    return n


def _parse_args(argv=None, *,
                description="Re-extract adsb.lol trace-days the old segmenter fused at a missed landing.",
                execute_help="Delete ledger rows and re-extract. Without it this is a dry run.",
                ) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=description)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--execute", action="store_true", help=execute_help)
    g.add_argument("--dry-run", action="store_true",
                   help="Default: print per-day pair counts and exit, mutating nothing.")
    p.add_argument("--days", type=_nonneg_int, default=None,
                   help="Limit to the first N affected days (pilot run).")
    p.add_argument("--accept-missing", action="store_true",
                   help="Don't fail the run on missing traces (old rows still retained for them).")
    p.add_argument("--sweep-stale", action="store_true",
                   help="Only sweep re-keyed superseded bronze rows left by an interrupted run, "
                        "then exit.")
    return p.parse_args(argv)


def cli(argv=None, *, affected=None, **parser_kw) -> int:
    # One wave CLI for every selector script; class1 passes its own selector + help strings.
    args = _parse_args(argv, **parser_kw)
    if args.sweep_stale:
        sweep_stale(execute=args.execute)
        return 0
    return run(execute=args.execute, days_limit=args.days,
               accept_missing=args.accept_missing, affected=affected)


def main(argv=None) -> int:
    return cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
