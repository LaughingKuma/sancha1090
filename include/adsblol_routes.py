from __future__ import annotations

import gzip
import json
import math
import os
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from datetime import datetime, timezone
from typing import Any, Optional

import polars as pl
import requests

from include import manifest
from include import adsblol_route_ledger as ledger
from include.adsblol_backfill import _num, _trace_preamble
from include.s3_helpers import write_parquet

# Mirrors dbt legs_gap_min (90 min) so trace segments and fct_flight_legs sessions
# agree on what counts as one flight.
GAP_SPLIT_S = 5400
_MIN_FIXES = 2

# Mirrors dbt chain_low_fix_alt_m (300 m) / chain_low_fix_gap_min (45 min): a boundary fix this
# low is landing/departing; with a turnaround-sized gap the aircraft landed inside it.
LOW_FIX_ALT_FT = 984.0
LOW_FIX_GAP_S = 2700

# A turnaround-sized silence; below it a low ~20-min gap is holding/go-around, not a landing
# (the v6.18 trade, pinned by test_low_fix_short_gap_does_not_split).
SLOW_GAP_S = 1800
# Implied great-circle speed across the gap below this = the aircraft stopped inside it; kept
# <= dbt chain_speed_min_kmh (300) so the chain layer independently refuses to re-fuse these.
SLOW_GAP_SPEED_KMH = 100
# Mirrors dbt legs_cruise_alt_m (3000 m, the snap/overflight ceiling): a real landing descends
# through it, so cruise-level coverage voids (both fixes high) must not split.
SLOW_GAP_CEIL_FT = 9843.0

TRACE_URL = "https://globe.adsb.lol/globe_history/{y}/{m:02d}/{d:02d}/traces/{shard}/trace_full_{hexid}.json"
USER_AGENT = "sancha1090-routes"


_SESSION: Optional[requests.Session] = None


def _default_session() -> requests.Session:
    # Lazily-built shared session for standalone fetch_trace calls; run_daily passes its own.
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
    return _SESSION


def fetch_trace(day: date, hexid: str, *, session: Optional[requests.Session] = None,
                timeout: int = 30) -> Optional[dict[str, Any]]:
    url = TRACE_URL.format(y=day.year, m=day.month, d=day.day, shard=hexid[-2:], hexid=hexid)
    sess = session or _default_session()
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            # 404 = the aircraft has no trace that day — a fact, not an error.
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.content
            # adsb.lol serves gzip CONTENT under the bare .json name (requests only auto-decodes
            # Content-Encoding), so decompress the body bytes ourselves on the magic byte.
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return json.loads(data)
        except (requests.RequestException, json.JSONDecodeError,
                gzip.BadGzipFile, zlib.error, EOFError) as exc:
            last = exc
        time.sleep(2 ** attempt)
    raise RuntimeError(f"trace fetch kept failing for {url}: {last}")


RAW_SEGMENTS_SCHEMA = {
    "icao24": pl.Utf8,
    "callsign": pl.Utf8,
    "seg_start": pl.Int64,
    "seg_end": pl.Int64,
    "num_fixes": pl.Int64,
    "first_lat": pl.Float64,
    "first_lon": pl.Float64,
    "first_alt_ft": pl.Float64,
    "first_on_ground": pl.Boolean,
    "last_lat": pl.Float64,
    "last_lon": pl.Float64,
    "last_alt_ft": pl.Float64,
    "last_on_ground": pl.Boolean,
    "trace_day": pl.Utf8,
    "source": pl.Utf8,
}


def _haversine_km(lat1, lon1, lat2, lon2):
    # Great-circle distance for the slow-gap arm's implied cross-gap speed; R in km.
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _seg_break(t, prev_t, prev_on_ground, on_ground, prev_alt_ft, alt_ft,
               prev_lat, prev_lon, lat, lon):
    # One source for both walk loops (segments + paths) so their grouping can never drift.
    gap = t - prev_t
    if gap > GAP_SPLIT_S:
        return True
    if on_ground and prev_on_ground is False:
        return True
    lo = min(prev_alt_ft if prev_alt_ft is not None else 99999.0,
             alt_ft if alt_ft is not None else 99999.0)
    if gap >= LOW_FIX_GAP_S and lo < LOW_FIX_ALT_FT:
        return True
    # Slow-gap landing: a turnaround-sized silence, below the cruise ceiling, that the aircraft
    # crossed too slowly to have stayed airborne -> it landed inside the gap even when no
    # ground/low fix bookends it. Both guards must hold, or a cruise-level or fast crossing splits.
    # Paths persist whole-second ts, so this arm evaluates on that integer grid — AFFECTED_SQL is
    # formula-identical (SQL-selected iff Python-splits) so the backfill's dry-run converges to zero.
    # Also fragments parked stretches at 30-min silences; all-ground pieces then drop at the keep-filter.
    gap_i = int(t) - int(prev_t)
    return (gap_i >= SLOW_GAP_S and lo < SLOW_GAP_CEIL_FT
            and _haversine_km(prev_lat, prev_lon, lat, lon) / (gap_i / 3600.0) < SLOW_GAP_SPEED_KMH)


def _parse_point(point, base):
    # One source for both walk loops (segments + paths) so their per-point parse/reject can never drift.
    t = base + float(point[0])
    flags = point[6] if len(point) > 6 and isinstance(point[6], int) else 0
    # flags&1 = repeated last-known fix: identity fill, not a position.
    if flags & 1:
        return None
    lat, lon = _num(point[1]), _num(point[2])
    if lat is None or lon is None:
        return None
    alt_raw = point[3] if len(point) > 3 else None
    on_ground = alt_raw == "ground"
    alt_ft = 0.0 if on_ground else _num(alt_raw)
    return t, lat, lon, alt_ft, on_ground


def trace_segments(trace_doc: dict[str, Any], day: date) -> list[dict[str, Any]]:
    preamble = _trace_preamble(trace_doc)
    if preamble is None:
        return []
    points, icao, base = preamble

    segs: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    prev_t: Optional[float] = None
    prev_on_ground: Optional[bool] = None
    prev_alt_ft: Optional[float] = None
    prev_lat: Optional[float] = None
    prev_lon: Optional[float] = None

    for point in points:
        parsed = _parse_point(point, base)
        if parsed is None:
            continue
        t, lat, lon, alt_ft, on_ground = parsed
        extra = point[8] if len(point) > 8 else None
        flight = (extra.get("flight") or "").strip() if isinstance(extra, dict) else ""

        # Same session breaks as fct_flight_legs: long gap, or ground contact after air
        # (the landing's ground fix opens the next segment, at the arrival airport). The
        # low-fix and slow-gap arms catch landings whose ground fix never appears in the trace.
        if cur is None or _seg_break(t, prev_t, prev_on_ground, on_ground, prev_alt_ft, alt_ft,
                                     prev_lat, prev_lon, lat, lon):
            if cur is not None:
                segs.append(cur)
            cur = {"first": (t, lat, lon, alt_ft, on_ground), "callsigns": {}, "n": 0, "air": 0}
        cur["last"] = (t, lat, lon, alt_ft, on_ground)
        cur["n"] += 1
        cur["air"] += 0 if on_ground else 1
        if flight:
            cur["callsigns"][flight] = cur["callsigns"].get(flight, 0) + 1
        prev_t, prev_on_ground, prev_alt_ft, prev_lat, prev_lon = t, on_ground, alt_ft, lat, lon

    if cur is not None:
        segs.append(cur)

    rows: list[dict[str, Any]] = []
    for s in segs:
        # Parked/taxi-only clusters aren't flights.
        if s["n"] < _MIN_FIXES or s["air"] == 0:
            continue
        callsign = (
            sorted(s["callsigns"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            if s["callsigns"] else None
        )
        ft, flat, flon, falt, fgnd = s["first"]
        lt, llat, llon, lalt, lgnd = s["last"]
        rows.append({
            "icao24": icao,
            "callsign": callsign,
            "seg_start": int(ft),
            "seg_end": int(lt),
            "num_fixes": s["n"],
            "first_lat": flat, "first_lon": flon, "first_alt_ft": falt, "first_on_ground": fgnd,
            "last_lat": llat, "last_lon": llon, "last_alt_ft": lalt, "last_on_ground": lgnd,
            "trace_day": day.isoformat(),
            "source": "adsblol",
        })
    return rows


RAW_PATHS_SCHEMA = {
    "icao24": pl.Utf8,
    "seg_start": pl.Int64,
    "ts": pl.Int64,
    "lat": pl.Float64,
    "lon": pl.Float64,
    "alt_ft": pl.Float64,
    "on_ground": pl.Boolean,
    "gs_kt": pl.Float64,
    "track_deg": pl.Float64,
    "trace_day": pl.Utf8,
    "source": pl.Utf8,
}


def trace_paths(trace_doc: dict[str, Any], day: date,
                segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Capture-only full paths: the same trace pass keeps every kept segment's fixes —
    # retrofitting later would mean re-streaming the whole tarball backlog.
    preamble = _trace_preamble(trace_doc)
    if preamble is None or not segments:
        return []
    points, icao, base = preamble
    # An int-second [seg_start, seg_end] re-check misbins fixes at split boundaries
    # (truncation collisions); re-walk trace_segments' exact rule and key off the group.
    keep_starts = {s["seg_start"] for s in segments}

    rows: list[dict[str, Any]] = []
    group_start: Optional[int] = None
    prev_t: Optional[float] = None
    prev_on_ground: Optional[bool] = None
    prev_alt_ft: Optional[float] = None
    prev_lat: Optional[float] = None
    prev_lon: Optional[float] = None
    for point in points:
        parsed = _parse_point(point, base)
        if parsed is None:
            continue
        t, lat, lon, alt_ft, on_ground = parsed

        if group_start is None or _seg_break(t, prev_t, prev_on_ground, on_ground, prev_alt_ft, alt_ft,
                                             prev_lat, prev_lon, lat, lon):
            group_start = int(t)
        prev_t, prev_on_ground, prev_alt_ft, prev_lat, prev_lon = t, on_ground, alt_ft, lat, lon

        if group_start not in keep_starts:
            continue
        rows.append({
            "icao24": icao,
            "seg_start": group_start,
            "ts": int(t),
            "lat": lat, "lon": lon,
            "alt_ft": alt_ft,
            "on_ground": on_ground,
            "gs_kt": _num(point[4]) if len(point) > 4 else None,
            "track_deg": _num(point[5]) if len(point) > 5 else None,
            "trace_day": day.isoformat(),
            "source": "adsblol",
        })
    return rows


def _frame(rows: list[dict[str, Any]], schema: dict) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
    return df.with_columns(pl.lit(datetime.now(timezone.utc).isoformat()).alias("ingested_at"))


def segments_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return _frame(rows, RAW_SEGMENTS_SCHEMA)


def paths_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return _frame(rows, RAW_PATHS_SCHEMA)


# ~4 req/s: polite pacing against globe.adsb.lol's static hosting.
FETCH_SPACING_S = 0.25


def route_targets(day: date, *, client=None) -> list[str]:
    from include.clickhouse import ch_client

    gold = os.environ.get("CH_GOLD_SCHEMA", "gold_ch")
    c = client or ch_client()
    try:
        # Overlap on either endpoint: a flight landing on 'day' but departing 'day-1' must be
        # targeted on the 'day' run so run_daily's (day, day-1) fetch grabs its arrival trace.
        rows = c.query(
            f"SELECT DISTINCT lower(icao24) FROM {gold}.fct_flights_reconciled "
            f"WHERE (origin_icao IS NULL OR dest_icao IS NULL) "
            f"AND (toDate(start_time) = %(day)s OR toDate(end_time) = %(day)s) "
            f"AND icao24 IS NOT NULL",
            parameters={"day": day.isoformat()},
        ).result_rows
    finally:
        if client is None:
            c.close()
    return sorted({r[0] for r in rows if r[0]})


def _fetch_pair(fetch, session, hexid: str, iso_day: str, spacing_s: float):
    # One pair's whole unit of work (fetch + politeness sleep + segmentation), returning its rows
    # and attempt outcome so the caller aggregates on a single thread — workers never touch shared
    # state. spacing_s is paid inside every branch so the effective rate is ~workers/spacing.
    d = date.fromisoformat(iso_day)
    try:
        doc = fetch(d, hexid, session=session)
    except RuntimeError:
        # One persistently-failing pair must not discard the rest of the run's fetches.
        time.sleep(spacing_s)
        return [], [], (hexid, iso_day, "error")
    if doc is None:
        time.sleep(spacing_s)
        return [], [], (hexid, iso_day, "missing")
    segs = trace_segments(doc, d)
    paths = trace_paths(doc, d, segs)
    time.sleep(spacing_s)
    return segs, paths, (hexid, iso_day, "landed")


def _run_serial(pairs, fetch, spacing_s, progress, total):
    rows: list[dict[str, Any]] = []
    path_rows: list[dict[str, Any]] = []
    attempts: list[tuple[str, str, str]] = []
    # One session per run reuses the TLS connection across every fetch (the bulk of per-pair cost).
    session = requests.Session()
    done = 0
    try:
        for hexid, iso_day in pairs:
            segs, paths, attempt = _fetch_pair(fetch, session, hexid, iso_day, spacing_s)
            rows.extend(segs)
            path_rows.extend(paths)
            attempts.append(attempt)
            done += 1
            if progress is not None:
                progress(done, total)
    finally:
        session.close()
    return rows, path_rows, attempts


def _run_concurrent(pairs, fetch, spacing_s, workers, progress, total):
    rows: list[dict[str, Any]] = []
    path_rows: list[dict[str, Any]] = []
    attempts: list[tuple[str, str, str]] = []
    # Sessions aren't thread-safe, so each worker thread lazily builds and reuses its own (still
    # keep-alive within the thread); all are tracked for close at the end.
    tls = threading.local()
    sessions: list[requests.Session] = []
    sessions_lock = threading.Lock()

    def worker(pair):
        sess = getattr(tls, "session", None)
        if sess is None:
            sess = requests.Session()
            tls.session = sess
            with sessions_lock:
                sessions.append(sess)
        return _fetch_pair(fetch, sess, pair[0], pair[1], spacing_s)

    done = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            # Aggregate on the main thread as futures complete — the only writer of the shared lists.
            for fut in as_completed([ex.submit(worker, p) for p in pairs]):
                segs, paths, attempt = fut.result()
                rows.extend(segs)
                path_rows.extend(paths)
                attempts.append(attempt)
                done += 1
                if progress is not None:
                    progress(done, total)
    finally:
        for s in sessions:
            s.close()
    return rows, path_rows, attempts


def run_daily(day: date, targets: Optional[list[str]] = None, *, engine=None, fetch=None,
              spacing_s: float = FETCH_SPACING_S, progress=None, workers: int = 1) -> dict:
    fetch = fetch or fetch_trace
    # None keeps the scheduled route_targets(day) selection; a list is the backfill's re-segment
    # target set, still flowing through filter_unattempted + record_attempts below.
    hexes = targets if targets is not None else route_targets(day)
    # Scheduled path also fetches D-1 (midnight-spanning departures live in the prior day's trace);
    # explicit targets re-segment self-contained trace-days, so D-1 there is pure double-fetch.
    days = (day,) if targets is not None else (day, day - timedelta(days=1))
    pairs = [(h, d.isoformat()) for h in hexes for d in days]
    pairs = ledger.filter_unattempted(pairs, engine)

    total = len(pairs)
    # workers=1 = the scheduled DAG's byte-identical serial loop; >1 = the backfill's concurrent lane.
    if workers > 1:
        rows, path_rows, attempts = _run_concurrent(pairs, fetch, spacing_s, workers, progress, total)
    else:
        rows, path_rows, attempts = _run_serial(pairs, fetch, spacing_s, progress, total)

    df = segments_frame(rows)
    pdf = paths_frame(path_rows)
    # Outcome tally lets the backfill print a per-day landed/missing/errors line.
    result = {"targets": len(hexes), "fetched": len(pairs),
              "rows": df.height, "path_rows": pdf.height, "uri": None,
              "landed": sum(1 for _, _, o in attempts if o == "landed"),
              "missing": sum(1 for _, _, o in attempts if o == "missing"),
              "errors": sum(1 for _, _, o in attempts if o == "error"),
              # Landed hexes only: the backfill deletes their superseded bronze rows (a re-walk that
              # drops a landing's leading ground cluster gets a new seg_start the RMT won't replace).
              "landed_hexes": sorted({h for h, _, o in attempts if o == "landed"})}
    # One stamp per run: a same-day rerun lands additively (record_load keeps ch_loaded_at on a
    # same-key rewrite, so overwriting part-000 never re-drained); the RMT dedups overlaps.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    if df.height:
        key = f"bronze/adsblol_flight_segments/dt={day.isoformat()}/part-{stamp}.parquet"
        uri = write_parquet(df, key)
        starts = df.get_column("seg_start")
        manifest.record_load(uri, int(starts.min()), int(starts.max()), df.height, engine=engine)
        result["uri"] = uri
    if pdf.height:
        pkey = f"bronze/adsblol_flight_paths/dt={day.isoformat()}/part-{stamp}.parquet"
        puri = write_parquet(pdf, pkey)
        ts = pdf.get_column("ts")
        manifest.record_load(puri, int(ts.min()), int(ts.max()), pdf.height, engine=engine)
    ledger.record_attempts(attempts, engine)
    return result
