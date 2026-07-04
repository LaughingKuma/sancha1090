from __future__ import annotations

import gzip
import tarfile
import zlib
from datetime import date, datetime, timedelta, timezone
from typing import Any, BinaryIO, Callable, Iterator, Optional

KT_TO_MPS = 0.514444
FT_TO_M = 0.3048
FPM_TO_MPS = 0.00508

# Mirrors include/regions.py REGIONS[0]; tests/test_regions_sync.py-style drift is
# guarded in tests/test_adsblol_backfill.py.
JAPAN_BBOX = (20.0, 122.0, 50.0, 165.0)

# Full resolution over the WHOLE Japan box (storage budgeted for it): every trace
# point is kept. kanto/kansai are labels, not filters — matched first so the home
# sky and the KIX hub stay trivially selectable downstream; japan_dense catches
# the rest of the box. (lamin, lomin, lamax, lomax)
DENSE_ZONES: dict[str, tuple[float, float, float, float]] = {
    "kanto": (34.5, 138.5, 36.8, 141.5),
    "kansai": (33.8, 134.5, 35.3, 136.5),
    "japan_dense": JAPAN_BBOX,
}

SNAPSHOT_INTERVAL_S = 720
# A point this stale no longer represents "where the aircraft is now" — mirrors
# OpenSky's own SV-staleness guidance (WHERE time-lastcontact<=15, relaxed for
# the sparser community-feeder coverage).
STALENESS_S = 60

GITHUB_RELEASE_URL = "https://github.com/adsblol/{repo}/releases/download/{tag}/{tag}.tar{suffix}"


def release_candidates(day: date) -> list[tuple[str, str]]:
    # Year-boundary days are published in the adjacent year's repo, and some days
    # carry -0tmp or staging tags (e.g. 2025-06-01, 2026-05-06) — probe in order.
    tags = [
        f"v{day.year}.{day.month:02d}.{day.day:02d}-planes-readsb-prod-0",
        f"v{day.year}.{day.month:02d}.{day.day:02d}-planes-readsb-prod-0tmp",
        f"v{day.year}.{day.month:02d}.{day.day:02d}-planes-readsb-staging-0",
    ]
    repos = [f"globe_history_{day.year}", f"globe_history_{day.year + 1}"]
    return [(repo, tag) for tag in tags for repo in repos]


def part_url(repo: str, tag: str, part: str = "") -> str:
    # Days under the 2 GB split threshold ship as a single unsplit .tar (no .aa).
    return GITHUB_RELEASE_URL.format(repo=repo, tag=tag, suffix=f".{part}" if part else "")


class ChainedReader:
    # tarfile in 'r|' mode needs one sequential file-like over the .tar.aa/.tar.ab/...
    # split; parts open lazily so later connections don't idle-timeout while the
    # earlier multi-GB part is still streaming.
    def __init__(self, openers: list[Callable[[], BinaryIO]]):
        self._openers = list(openers)
        self._idx = 0
        self._current: Optional[BinaryIO] = None

    def read(self, n: int = -1) -> bytes:
        # read(0) must be a no-op: the part-EOF advance below would otherwise
        # misread the empty chunk and close every part.
        if n == 0:
            return b""
        chunks: list[bytes] = []
        remaining = n
        while self._idx < len(self._openers):
            if self._current is None:
                self._current = self._openers[self._idx]()
            chunk = self._current.read(remaining if remaining >= 0 else -1)
            if chunk:
                chunks.append(chunk)
                if remaining >= 0:
                    remaining -= len(chunk)
                    if remaining <= 0:
                        break
            else:
                self._current.close()
                self._current = None
                self._idx += 1
        return b"".join(chunks)

    def close(self) -> None:
        if self._current is not None:
            self._current.close()
            self._current = None
        self._idx = len(self._openers)


def iter_trace_members(stream: Any) -> Iterator[tuple[str, Optional[bytes]]]:
    try:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                if not member.isfile() or "trace_full_" not in member.name:
                    continue
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                data = fobj.read()
                # Trace files are gzip despite the bare .json name.
                if data[:2] == b"\x1f\x8b":
                    try:
                        data = gzip.decompress(data)
                    except (zlib.error, gzip.BadGzipFile, EOFError):
                        # One corrupt member must not kill a 60k-trace day; the
                        # caller counts these and fails the day if they cascade
                        # (a desynced tar stream corrupts everything after it).
                        yield member.name, None
                        continue
                yield member.name, data
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()


def member_icao(name: str) -> Optional[str]:
    # '~'-prefixed addresses are TIS-B/non-ICAO synthetics — not real airframes.
    base = name.rsplit("trace_full_", 1)[-1]
    hexid = base.split(".json", 1)[0].lower()
    if not hexid or hexid.startswith("~"):
        return None
    return hexid


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def _trace_preamble(trace_doc: dict[str, Any]) -> Optional[tuple[list[Any], str, float]]:
    points = trace_doc.get("trace") or []
    if not points:
        return None
    icao = (trace_doc.get("icao") or "").lower()
    base = trace_doc.get("timestamp")
    if not icao or icao.startswith("~") or base is None:
        return None
    return points, icao, base


def _point_row(
    icao: str,
    callsign: Optional[str],
    squawk: Optional[str],
    t: float,
    point: list[Any],
    flags: int,
    lat: float,
    lon: float,
    snapshot_time: int,
    region: str,
) -> dict[str, Any]:
    alt_baro = point[3] if len(point) > 3 else None
    on_ground = alt_baro == "ground"
    gs = _num(point[4]) if len(point) > 4 else None
    track = _num(point[5]) if len(point) > 5 else None
    baro_rate = _num(point[7]) if len(point) > 7 else None
    alt_geom = _num(point[10]) if len(point) > 10 else None
    alt_main = None if on_ground else _num(alt_baro)
    # flags&8 = the altitude field is geometric, not barometric.
    alt_is_geom = bool(flags & 8)

    return {
        "icao24": icao,
        "callsign": callsign,
        "origin_country": None,
        "time_position": int(t),
        "last_contact": int(t),
        "longitude": lon,
        "latitude": lat,
        "baro_altitude": None if alt_is_geom or alt_main is None else alt_main * FT_TO_M,
        "on_ground": on_ground,
        "velocity": gs * KT_TO_MPS if gs is not None else None,
        "true_track": track,
        # flags&4 marks a geometric rate — still a vertical rate, keep it.
        "vertical_rate": baro_rate * FPM_TO_MPS if baro_rate is not None else None,
        "geo_altitude": (
            alt_geom * FT_TO_M if alt_geom is not None
            else (alt_main * FT_TO_M if alt_is_geom and alt_main is not None else None)
        ),
        "squawk": squawk,
        "spi": None,
        "position_source": None,
        "snapshot_time": snapshot_time,
        "region": region,
        "source": "adsblol",
    }


def resample_trace(
    trace_doc: dict[str, Any],
    day_start: int,
    interval_s: int = SNAPSHOT_INTERVAL_S,
    staleness_s: int = STALENESS_S,
    bbox: tuple[float, float, float, float] = JAPAN_BBOX,
) -> list[dict[str, Any]]:
    preamble = _trace_preamble(trace_doc)
    if preamble is None:
        return []
    points, icao, base = preamble

    lamin, lomin, lamax, lomax = bbox
    # k starts at 1: a 00:00 boundary could only be satisfied by the PREVIOUS
    # day's file, so the day owns 00:12 .. 24:00 and days tile without holes.
    boundaries = [day_start + k * interval_s for k in range(1, 86400 // interval_s + 1)]

    rows: list[dict[str, Any]] = []
    callsign: Optional[str] = None
    squawk: Optional[str] = None
    idx = 0
    chosen: Optional[tuple[float, list[Any], int]] = None

    for boundary in boundaries:
        # Points are time-ordered; advance one shared cursor across all boundaries.
        while idx < len(points):
            point = points[idx]
            t = base + float(point[0])
            if t > boundary:
                break
            extra = point[8] if len(point) > 8 else None
            if isinstance(extra, dict):
                flight = (extra.get("flight") or "").strip()
                if flight:
                    callsign = flight
                if extra.get("squawk"):
                    squawk = str(extra["squawk"])
            flags = point[6] if len(point) > 6 and isinstance(point[6], int) else 0
            # flags&1 = repeated last-known fix: fine for identity fill, not position.
            if not flags & 1:
                chosen = (t, point, flags)
            idx += 1

        if chosen is None:
            continue
        t, point, flags = chosen
        if boundary - t > staleness_s:
            continue
        lat, lon = _num(point[1]), _num(point[2])
        if lat is None or lon is None:
            continue
        if not (lamin <= lat <= lamax and lomin <= lon <= lomax):
            continue

        rows.append(_point_row(icao, callsign, squawk, t, point, flags, lat, lon, boundary, "japan"))

    return rows


def dense_rows(
    trace_doc: dict[str, Any],
    day_start: int,
    zones: dict[str, tuple[float, float, float, float]] = DENSE_ZONES,
) -> list[dict[str, Any]]:
    preamble = _trace_preamble(trace_doc)
    if preamble is None:
        return []
    points, icao, base = preamble

    day_end = day_start + 86400
    rows: list[dict[str, Any]] = []
    callsign: Optional[str] = None
    squawk: Optional[str] = None

    for point in points:
        t = base + float(point[0])
        extra = point[8] if len(point) > 8 else None
        if isinstance(extra, dict):
            flight = (extra.get("flight") or "").strip()
            if flight:
                callsign = flight
            if extra.get("squawk"):
                squawk = str(extra["squawk"])
        # Clamp to the day so adjacent waves tile without duplicate points.
        if not (day_start <= t < day_end):
            continue
        flags = point[6] if len(point) > 6 and isinstance(point[6], int) else 0
        # flags&1 = repeated last-known fix: fine for identity fill, not position.
        if flags & 1:
            continue
        lat, lon = _num(point[1]), _num(point[2])
        if lat is None or lon is None:
            continue
        # First match wins (dict order): named zones before the japan_dense catch-all.
        zone = next(
            (name for name, (lamin, lomin, lamax, lomax) in zones.items()
             if lamin <= lat <= lamax and lomin <= lon <= lomax),
            None,
        )
        if zone is None:
            continue

        rows.append(_point_row(icao, callsign, squawk, t, point, flags, lat, lon, int(t), zone))

    return rows


def flights_windows(
    until_day: date,
    from_day: date,
    window_days: int = 2,
) -> Iterator[tuple[date, int, int]]:
    # Newest-first so the most dashboard-relevant history lands before the credit
    # drip runs dry on any given day.
    w_end = until_day + timedelta(days=1)
    while w_end > from_day:
        w_begin = max(from_day, w_end - timedelta(days=window_days))
        begin_ts = int(datetime(w_begin.year, w_begin.month, w_begin.day, tzinfo=timezone.utc).timestamp())
        end_ts = int(datetime(w_end.year, w_end.month, w_end.day, tzinfo=timezone.utc).timestamp())
        yield w_begin, begin_ts, end_ts
        w_end = w_begin
