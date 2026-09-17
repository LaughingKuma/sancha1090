from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import polars as pl

from include.adsblol_trace_utils import num, trace_preamble

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

# A ground bit the transponder never sets keeps a whole turnaround airborne (AAL61 at KDFW, MH691 at RJTT):
# >= 30 min under 30 kt below LOW_FIX_ALT_FT is that signal; the altitude guard spares balloons and hovers.
DWELL_GS_KT = 30.0
DWELL_S = 1800

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


@dataclass(slots=True)
class _Fix:
    t: float
    lat: float
    lon: float
    alt_ft: float | None
    on_ground: bool
    gs_kt: float | None
    point: list
    takeoff: bool = False


def _seg_break(prev: _Fix, fix: _Fix):
    # _iter_groups is the only caller; the low-fix and slow-gap arms catch landings with no ground fix.
    if fix.takeoff:
        return True
    gap = fix.t - prev.t
    if gap > GAP_SPLIT_S:
        return True
    if fix.on_ground and prev.on_ground is False:
        return True
    lo = min(prev.alt_ft if prev.alt_ft is not None else 99999.0,
             fix.alt_ft if fix.alt_ft is not None else 99999.0)
    if gap >= LOW_FIX_GAP_S and lo < LOW_FIX_ALT_FT:
        return True
    # Slow-gap landing: a turnaround-sized silence, below the cruise ceiling, that the aircraft
    # crossed too slowly to have stayed airborne -> it landed inside the gap even when no
    # ground/low fix bookends it. Both guards must hold, or a cruise-level or fast crossing splits.
    # Paths persist whole-second ts, so this arm evaluates on that integer grid — SLOW_GAP_SQL is
    # formula-identical (SQL-selected iff Python-splits) so the backfill's dry-run converges to zero.
    # Also fragments parked stretches at 30-min silences; all-ground pieces then drop at the keep-filter.
    gap_i = int(fix.t) - int(prev.t)
    return (gap_i >= SLOW_GAP_S and lo < SLOW_GAP_CEIL_FT
            and _haversine_km(prev.lat, prev.lon, fix.lat, fix.lon) / (gap_i / 3600.0) < SLOW_GAP_SPEED_KMH)


def _parse_point(point, base):
    t = base + float(point[0])
    flags = point[6] if len(point) > 6 and isinstance(point[6], int) else 0
    # flags&1 = repeated last-known fix: identity fill, not a position.
    if flags & 1:
        return None
    lat, lon = num(point[1]), num(point[2])
    if lat is None or lon is None:
        return None
    alt_raw = point[3] if len(point) > 3 else None
    on_ground = alt_raw == "ground"
    alt_ft = 0.0 if on_ground else num(alt_raw)
    gs_kt = num(point[4]) if len(point) > 4 else None
    return t, lat, lon, alt_ft, on_ground, gs_kt


def _dwell_fix(f: _Fix):
    return (f.alt_ft is not None and f.alt_ft < LOW_FIX_ALT_FT
            and f.gs_kt is not None and f.gs_kt < DWELL_GS_KT)


def _runs(fixes, pred):
    # Maximal pred runs spanning >= DWELL_S on the persisted integer grid (DWELL_SQL parity), cut at a
    # SLOW_GAP_S silence: the walk drops the all-ground piece there, so a selector could never see across it.
    i, n = 0, len(fixes)
    while i < n:
        if not pred(fixes[i]):
            i += 1
            continue
        j = i
        while j + 1 < n and pred(fixes[j + 1]) and int(fixes[j + 1].t) - int(fixes[j].t) < SLOW_GAP_S:
            j += 1
        if int(fixes[j].t) - int(fixes[i].t) >= DWELL_S:
            yield i, j
        i = j + 1


def _persisted(fixes, pred):
    # Paths persist one row per whole second, last write wins: a run predicate must read every fix of a
    # second as its last fix does, or the selector sees a dwell the walk never splits and never converges.
    last = {}
    for k, f in enumerate(fixes):
        last[int(f.t)] = k
    return lambda f: pred(fixes[last[int(f.t)]])


def _parse_trace(points, base) -> list[_Fix]:
    # One pass feeds the group walk: the dwell read needs the whole run before the walk sees a fix.
    fixes = []
    for point in points:
        parsed = _parse_point(point, base)
        if parsed is not None:
            fixes.append(_Fix(*parsed, point))
    for i, j in _runs(fixes, _persisted(fixes, _dwell_fix)):
        for f in fixes[i:j + 1]:
            f.on_ground = True
    # Takeoff trim: the last ground fix of a >= DWELL_S ground run opens the departure segment (DAL121: 16 h
    # parked inside one flight); a run that ends the trace has no departure to open.
    for i, j in _runs(fixes, _persisted(fixes, lambda f: f.on_ground)):
        if j + 1 < len(fixes):
            fixes[j].takeoff = True
            # The roll before the trim point is not a flight: a ground-bit flicker inside a second the grid reads as
            # ground (71be22 06-24) must not keep it; an arrival's last airborne fix sharing the landing second stays.
            k = next(k for k in range(i, j + 1) if fixes[k].on_ground)
            for f in fixes[k:j]:
                f.on_ground = True
    return fixes


def _iter_groups(points, base):
    # Same session breaks as fct_flight_legs (the landing's ground fix opens the next group, at the
    # arrival airport); the one walk feeds both segments and paths, so their grouping cannot drift.
    group: list[_Fix] = []
    prev = None
    for fix in _parse_trace(points, base):
        if prev is not None and _seg_break(prev, fix):
            yield group
            group = []
        group.append(fix)
        prev = fix
    if group:
        yield group


def trace_rows(trace_doc: dict[str, Any], day: date
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # Capture-only full paths ride the same walk as the segments: retrofitting later would mean
    # re-streaming the whole tarball backlog, and one walk means their grouping cannot drift.
    preamble = trace_preamble(trace_doc)
    if preamble is None:
        return [], []
    points, icao, base = preamble
    day_iso = day.isoformat()

    segments: list[dict[str, Any]] = []
    groups: list[list[_Fix]] = []
    for group in _iter_groups(points, base):
        groups.append(group)
        n = len(group)
        air = sum(1 for f in group if not f.on_ground)
        # Parked/taxi-only clusters aren't flights.
        if n < _MIN_FIXES or air == 0:
            continue
        callsigns: dict[str, int] = {}
        for f in group:
            extra = f.point[8] if len(f.point) > 8 else None
            flight = (extra.get("flight") or "").strip() if isinstance(extra, dict) else ""
            if flight:
                callsigns[flight] = callsigns.get(flight, 0) + 1
        callsign = (
            sorted(callsigns.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            if callsigns else None
        )
        first, last = group[0], group[-1]
        segments.append({
            "icao24": icao,
            "callsign": callsign,
            "seg_start": int(first.t),
            "seg_end": int(last.t),
            "num_fixes": n,
            "first_lat": first.lat, "first_lon": first.lon,
            "first_alt_ft": first.alt_ft, "first_on_ground": first.on_ground,
            "last_lat": last.lat, "last_lon": last.lon,
            "last_alt_ft": last.alt_ft, "last_on_ground": last.on_ground,
            "trace_day": day_iso,
            "source": "adsblol",
        })

    # Paths key off the group's start second, not the keep filter: a dropped piece opening inside a kept
    # segment's second (ground-bit flapping on rollout) persists under it; re-keying needs its own evidence.
    keep_starts = {s["seg_start"] for s in segments}
    paths: list[dict[str, Any]] = []
    for group in groups:
        seg_start = int(group[0].t)
        if seg_start not in keep_starts:
            continue
        for f in group:
            paths.append({
                "icao24": icao,
                "seg_start": seg_start,
                "ts": int(f.t),
                "lat": f.lat, "lon": f.lon,
                "alt_ft": f.alt_ft,
                "on_ground": f.on_ground,
                "gs_kt": f.gs_kt,
                "track_deg": num(f.point[5]) if len(f.point) > 5 else None,
                "trace_day": day_iso,
                "source": "adsblol",
            })
    return segments, paths


def trace_segments(trace_doc: dict[str, Any], day: date) -> list[dict[str, Any]]:
    return trace_rows(trace_doc, day)[0]


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
    if not segments:
        return []
    keep_starts = {s["seg_start"] for s in segments}
    return [r for r in trace_rows(trace_doc, day)[1] if r["seg_start"] in keep_starts]


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
