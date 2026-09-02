from __future__ import annotations

import logging
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import polars as pl

from include.adsblol_backfill import _num, _trace_preamble

log = logging.getLogger(__name__)

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


_HEX_RE = re.compile(r"^[0-9a-f]{6}$")


def _hexes(sql: str, params: dict, *, client=None) -> list[str]:
    from include.clickhouse import ch_client

    c = client or ch_client()
    try:
        rows = c.query(sql, parameters=params).result_rows
    finally:
        if client is None:
            c.close()
    # Bronze has zero non-ICAO hexes today; the SQL match() and this regex re-check are both belts
    # so a producer schema change can't leak junk fetch targets.
    return sorted({r[0] for r in rows if r[0] and _HEX_RE.match(r[0])})


def route_targets(day: date, *, client=None) -> list[str]:
    gold = os.environ.get("CH_GOLD_SCHEMA", "gold_ch")
    # Overlap on either endpoint: a flight landing on 'day' but departing 'day-1' must be
    # targeted on the 'day' run — the release lane extracts D's tar for D and D+1 targets.
    # Every reconciled flight qualifies (rung 1): endpoint-NULL-only targeting starved
    # fct_flight_path once SWIM resolved O/D pre-departure; the attempt ledger self-limits.
    return _hexes(
        f"SELECT DISTINCT lower(icao24) FROM {gold}.fct_flights_reconciled "
        f"WHERE (toDate(start_time) = %(day)s OR toDate(end_time) = %(day)s) "
        f"AND icao24 IS NOT NULL",
        {"day": day.isoformat()},
        client=client,
    )


def rooftop_cohort(day: date, *, client=None) -> list[str]:
    return _hexes(
        "SELECT DISTINCT lower(hex) FROM bronze.adsb_states "
        "WHERE capture_date = %(day)s AND hex IS NOT NULL "
        "AND match(lower(hex), '^[0-9a-f]{6}$')",
        {"day": day.isoformat()},
        client=client,
    )


def release_targets(day: date, *, client=None) -> list[str]:
    gold = os.environ.get("CH_GOLD_SCHEMA", "gold_ch")
    # Every flight reconciled on D was in our own states on D, and a D+1 flight starting before
    # midnight lives in D's trace; measured 08-20: 2,439 reconciled hexes in 3,557 state hexes, 0 residual.
    return _hexes(
        "SELECT DISTINCT h FROM ("
        "SELECT lower(hex) AS h FROM bronze.adsb_states "
        "WHERE capture_date IN (%(d0)s, %(d1)s) AND hex IS NOT NULL "
        "UNION ALL "
        "SELECT lower(icao24) FROM bronze.opensky_states "
        "WHERE toDate(snapshot_time) IN (%(d0)s, %(d1)s) AND icao24 IS NOT NULL "
        "UNION ALL "
        f"SELECT lower(icao24) FROM {gold}.fct_flights_reconciled "
        "WHERE (toDate(start_time) IN (%(d0)s, %(d1)s) OR toDate(end_time) IN (%(d0)s, %(d1)s)) "
        "AND icao24 IS NOT NULL"
        ") WHERE match(h, '^[0-9a-f]{6}$')",
        {"d0": day.isoformat(), "d1": (day + timedelta(days=1)).isoformat()},
        client=client,
    )
