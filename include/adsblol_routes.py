from __future__ import annotations

import gzip
import json
import os
import time
import urllib.error
import urllib.request
import zlib
from datetime import date, timedelta
from datetime import datetime, timezone
from typing import Any, Optional

import polars as pl

from include import manifest
from include import adsblol_route_ledger as ledger
from include.adsblol_backfill import _num, _trace_preamble
from include.s3_helpers import write_parquet

# Mirrors dbt legs_gap_min (90 min) so trace segments and fct_flight_legs sessions
# agree on what counts as one flight.
GAP_SPLIT_S = 5400
_MIN_FIXES = 2

TRACE_URL = "https://globe.adsb.lol/globe_history/{y}/{m:02d}/{d:02d}/traces/{shard}/trace_full_{hexid}.json"
USER_AGENT = "sancha1090-routes"


def fetch_trace(day: date, hexid: str, *, opener=urllib.request.urlopen,
                timeout: int = 30) -> Optional[dict[str, Any]]:
    url = TRACE_URL.format(y=day.year, m=day.month, d=day.day, shard=hexid[-2:], hexid=hexid)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            with opener(req, timeout=timeout) as resp:
                data = resp.read()
            # Served gzip regardless of the bare .json name (same as the tarball members).
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return json.loads(data)
        except urllib.error.HTTPError as exc:
            # 404 = the aircraft has no trace that day — a fact, not an error.
            if exc.code == 404:
                return None
            last = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
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


def trace_segments(trace_doc: dict[str, Any], day: date) -> list[dict[str, Any]]:
    preamble = _trace_preamble(trace_doc)
    if preamble is None:
        return []
    points, icao, base = preamble

    segs: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    prev_t: Optional[float] = None
    prev_on_ground: Optional[bool] = None

    for point in points:
        t = base + float(point[0])
        flags = point[6] if len(point) > 6 and isinstance(point[6], int) else 0
        # flags&1 = repeated last-known fix: identity fill, not a position.
        if flags & 1:
            continue
        lat, lon = _num(point[1]), _num(point[2])
        if lat is None or lon is None:
            continue
        alt_raw = point[3] if len(point) > 3 else None
        on_ground = alt_raw == "ground"
        alt_ft = 0.0 if on_ground else _num(alt_raw)
        extra = point[8] if len(point) > 8 else None
        flight = (extra.get("flight") or "").strip() if isinstance(extra, dict) else ""

        # Same session breaks as fct_flight_legs: long gap, or ground contact after air
        # (the landing's ground fix opens the next segment, at the arrival airport).
        if (
            cur is None
            or t - prev_t > GAP_SPLIT_S
            or (on_ground and prev_on_ground is False)
        ):
            if cur is not None:
                segs.append(cur)
            cur = {"first": (t, lat, lon, alt_ft, on_ground), "callsigns": {}, "n": 0, "air": 0}
        cur["last"] = (t, lat, lon, alt_ft, on_ground)
        cur["n"] += 1
        cur["air"] += 0 if on_ground else 1
        if flight:
            cur["callsigns"][flight] = cur["callsigns"].get(flight, 0) + 1
        prev_t, prev_on_ground = t, on_ground

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
    for point in points:
        t = base + float(point[0])
        flags = point[6] if len(point) > 6 and isinstance(point[6], int) else 0
        if flags & 1:
            continue
        lat, lon = _num(point[1]), _num(point[2])
        if lat is None or lon is None:
            continue
        alt_raw = point[3] if len(point) > 3 else None
        on_ground = alt_raw == "ground"

        if (
            group_start is None
            or t - prev_t > GAP_SPLIT_S
            or (on_ground and prev_on_ground is False)
        ):
            group_start = int(t)
        prev_t, prev_on_ground = t, on_ground

        if group_start not in keep_starts:
            continue
        rows.append({
            "icao24": icao,
            "seg_start": group_start,
            "ts": int(t),
            "lat": lat, "lon": lon,
            "alt_ft": 0.0 if on_ground else _num(alt_raw),
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


def run_daily(day: date, *, engine=None, fetch=None, spacing_s: float = FETCH_SPACING_S) -> dict:
    fetch = fetch or fetch_trace
    hexes = route_targets(day)
    # D-1 alongside D: a flight spanning UTC midnight has its departure in the prior day's trace.
    pairs = [(h, d.isoformat()) for h in hexes for d in (day, day - timedelta(days=1))]
    pairs = ledger.filter_unattempted(pairs, engine)

    rows: list[dict[str, Any]] = []
    path_rows: list[dict[str, Any]] = []
    attempts: list[tuple[str, str, str]] = []
    for hexid, iso_day in pairs:
        d = date.fromisoformat(iso_day)
        try:
            doc = fetch(d, hexid)
        except RuntimeError:
            # One persistently-failing pair must not discard the rest of the day's fetches.
            attempts.append((hexid, iso_day, "error"))
            time.sleep(spacing_s)
            continue
        if doc is None:
            attempts.append((hexid, iso_day, "missing"))
        else:
            segs = trace_segments(doc, d)
            rows.extend(segs)
            path_rows.extend(trace_paths(doc, d, segs))
            attempts.append((hexid, iso_day, "landed"))
        time.sleep(spacing_s)

    df = segments_frame(rows)
    pdf = paths_frame(path_rows)
    result = {"targets": len(hexes), "fetched": len(pairs),
              "rows": df.height, "path_rows": pdf.height, "uri": None}
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
