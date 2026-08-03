import asyncio
import collections
import contextlib
import datetime
import importlib.util
import json
import math
import os
import re
import time
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException


def _load_sibling(name):
    # file-relative: resolves in the baked image (/app) AND under spec-loaded tests alike
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


est = _load_sibling("estimator")
ess = _load_sibling("est_serving")
er = _load_sibling("est_route")
ladd = _load_sibling("ladd")
pf = _load_sibling("pathfusion")
cache = _load_sibling("cache")
rl = _load_sibling("ratelimit")
routes_live = _load_sibling("routes_live")
routes_aircraft = _load_sibling("routes_aircraft")
routes_path = _load_sibling("routes_path")

# Sibling surface re-exported under this module's names — this module stays the single address
# for the sidecar's behaviour; the config-bound seams further down bind the rest.
_EMPTY_SUPPRESS = ladd.EMPTY_SUPPRESS
_write_ladd_cache = ladd.write_cache
_read_ladd_cache = ladd.read_cache
_is_ladd_suppressed = ladd.is_suppressed
_ladd_filter_flights = ladd.filter_flights
PATH_HEAD_QUERY = pf.HEAD_QUERY
PATH_COMPETITOR_QUERY = pf.COMPETITOR_QUERY
PROVISIONAL_ADSB_QUERY = pf.ADSB_QUERY
PROVISIONAL_ADSBLOL_QUERY = pf.ADSBLOL_QUERY
PROVISIONAL_OPENSKY_QUERY = pf.OPENSKY_QUERY
_contest_keep = pf.contest_keep
_fuse_points = pf.fuse_points
_client_ip = rl.client_ip
_rate_limit_allow = rl.allow

# Server-side cache is the whole point: N browser tabs share ONE RW query stream, never N.
POLL_SECONDS = float(os.environ.get("LIVEMAP_POLL_SECONDS", "1.0"))
# Slow refreshes are tick-counted — derive the divisor so faster polls keep the ~5 min cadence
SLOW_REFRESH_TICKS = max(1, int(300 / POLL_SECONDS))
# v5.7: deque only backfills the ≤120 s /history wake — /track reads mv_track_positions
HISTORY_BUFFER_S = 120
RW_DSN = os.environ.get(
    "LIVEMAP_RW_DSN", "postgresql://root@risingwave:4566/dev"
)
# Bound the connect + query so a stalled RW can't freeze the poller and stale /healthz forever
DB_CONNECT_TIMEOUT = int(os.environ.get("LIVEMAP_DB_CONNECT_TIMEOUT", "3"))
DB_STATEMENT_TIMEOUT_MS = int(os.environ.get("LIVEMAP_DB_STATEMENT_TIMEOUT_MS", "3000"))

# CH read-only history source (superset_ro). Reachable on the compose network as clickhouse:8123.
CH_HOST = os.environ.get("LIVEMAP_CH_HOST", "clickhouse")
CH_PORT = int(os.environ.get("LIVEMAP_CH_PORT", "8123"))
CH_USER = os.environ.get("LIVEMAP_CH_USER", "superset_ro")
CH_PASSWORD = os.environ.get("LIVEMAP_CH_PASSWORD", "")
CH_DB = os.environ.get("LIVEMAP_CH_DB", "gold_ch")
CH_QUERY_TIMEOUT_S = int(os.environ.get("LIVEMAP_CH_QUERY_TIMEOUT_S", "4"))
CH_WRITER_USER = os.environ.get("LIVEMAP_CH_WRITER_USER", "livemap_writer")
CH_WRITER_PASSWORD = os.environ.get("LIVEMAP_CH_WRITER_PASSWORD", "")

# Bad logging settings must not prevent the serving process from importing.
try:
    EST_FLUSH_S = max(0.5, float(os.environ.get("LIVEMAP_EST_FLUSH_S", "5.0")))
except ValueError:
    EST_FLUSH_S = 5.0
try:
    EST_LOG_QUEUE_MAX = max(0, int(os.environ.get("LIVEMAP_EST_LOG_QUEUE_MAX", "256")))
except ValueError:
    EST_LOG_QUEUE_MAX = 256
try:
    EST_FLUSH_MAX_ROWS = max(1, int(os.environ.get("LIVEMAP_EST_FLUSH_MAX_ROWS", "1000")))
except ValueError:
    EST_FLUSH_MAX_ROWS = 1000

_est_log_queue = ess.LogQueue(EST_LOG_QUEUE_MAX)
_est_missing_table_warned = False
_est_route_fetch_warned: set = set()

LADD_REFRESH_SECONDS = float(os.environ.get("LIVEMAP_LADD_REFRESH_SECONDS", "900"))
LADD_REFRESH_TICKS = max(1, int(LADD_REFRESH_SECONDS / POLL_SECONDS))
LADD_CACHE_PATH = ladd.CACHE_PATH

# Real antenna is a secret (home rooftop) — from .env; default is the public Carrot Tower landmark.
FEEDER_LAT = float(os.environ.get("LIVEMAP_FEEDER_LAT", "35.6434"))
FEEDER_LON = float(os.environ.get("LIVEMAP_FEEDER_LON", "139.6692"))

# Public-instance hardening — every effect below is gated on this flag so the private LAN instance is
# byte-identical when it is unset (the middleware isn't even registered).
PUBLIC_MODE = os.environ.get("LIVEMAP_PUBLIC_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
# ruling 4 (2026-07-25): sidecar-attributed serving exhaust; legacy 'serving' = pre-split rows
EST_PRODUCER = "serving-public" if PUBLIC_MODE else "serving-private"
# Per-IP token bucket on the per-request DB endpoints only (/aircraft + /history are in-memory reads, free).
RATE_LIMITED_PREFIXES = ("/track/", "/flights/", "/path/", "/estimate/live/")

# recv rides the payload end-to-end (the P2 multi-receiver seam); rendered uniformly today.
QUERY = """
    SELECT capture_ts, hex, flight, lat, lon, alt_baro, gs, track,
           typecode, aircraft_desc, registration, body_class, is_military, is_helicopter, is_ladd,
           airline_name, reg_country, recv, own_op, year, category,
           squawk, position_source,
           baro_rate, geom_rate, rssi, nav_altitude_mcp, nav_modes
    FROM mv_current_aircraft
    WHERE lat IS NOT NULL AND lon IS NOT NULL
"""

# v5.7: 30-min trail from RW — survives sidecar restarts; row shape mirrors the old ring buffer
TRACK_QUERY = """
    SELECT lon, lat, capture_ts, alt_baro
    FROM mv_track_positions
    WHERE hex = %s
    ORDER BY capture_ts
"""

# "Where else has it been": one clean source now — the reconciled consensus mart (SP2) carries
# resolved O/D + endpoint geo + provenance, so no read-time fact_flights/legs UNION or watermark.
# flight_id is cityHash64 → UInt64; toString keeps it exact (JS Number can't hold it) and keys /path.
# LADD serve-time suppression is a PUBLIC-instance obligation (Amit ruling 2026-07-29) — the private LAN
# instance shows every airframe the antenna receives, so both mart-flag filters are selected at import.
_FLIGHTS_LADD_FILTER = "AND is_ladd = 0 " if PUBLIC_MODE else ""
_PATH_LADD_GATE = f"""
      AND flight_id IN (
        SELECT flight_id FROM {CH_DB}.fct_flights_reconciled
        WHERE flight_id = {{fid:UInt64}} AND is_ladd = 0
      )""" if PUBLIC_MODE else ""

FLIGHTS_QUERY = f"""
    SELECT 'reconciled' AS src, end_time AS ts,
           coalesce(origin_iata, origin_icao) AS o_code, coalesce(origin_city, origin_name) AS o_name,
           coalesce(dest_iata,   dest_icao)   AS d_code, coalesce(dest_city,   dest_name)   AS d_name, callsign,
           toString(flight_id) AS flight_id
    FROM {CH_DB}.fct_flights_reconciled
    WHERE icao24 = {{hex:String}} {_FLIGHTS_LADD_FILTER}AND (origin_icao IS NOT NULL OR dest_icao IS NOT NULL)
    ORDER BY ts DESC LIMIT 10
"""

# Historical fused trajectory for one reconciled flight; on public, LADD suppression rides the is_ladd=0
# subquery (window-aware) so a listed flight yields zero rows — indistinguishable from no-path, no oracle.
PATH_QUERY = f"""
    SELECT toUnixTimestamp(ts), lat, lon, alt_ft, on_ground, gs_kt, track_deg, source
    FROM {CH_DB}.fct_flight_path
    WHERE flight_id = {{fid:UInt64}}{_PATH_LADD_GATE}
    ORDER BY ts
"""

# Per-click authorization (also on geometry-cache hits): closes stale-LADD/stale-id windows without giving
# up the geometry cache; the window + start day ride along for the provisional arm (one execution).
PATH_AUTH_QUERY = f"""
    SELECT lower(icao24), callsign, is_ladd,
           toUnixTimestamp(start_time), toUnixTimestamp(end_time), toDate(start_time)
    FROM {CH_DB}.fct_flights_reconciled
    WHERE flight_id = {{fid:UInt64}}
    LIMIT 1
"""

# O/D + provenance for the estimate arm only (SP2 geo columns); /path never pays this query.
# The callsign/window ride the same read — the SWIM route prior needs them and one trip is enough.
PATH_OD_QUERY = f"""
    SELECT origin_lat, origin_lon, origin_source, origin_agreement,
           dest_lat, dest_lon, dest_source, dest_agreement,
           callsign, toUnixTimestamp(start_time), toUnixTimestamp(end_time),
           origin_icao, dest_icao
    FROM {CH_DB}.fct_flights_reconciled
    WHERE flight_id = {{fid:UInt64}}
    LIMIT 1
"""

# Filed SWIM plan (ledger 6a): O/D-anchored, conflict-vetoed; FULL plans authoritative by
# recency (newer tokenless full plan = real reroute -> GC); './.' fills only sans full plan.
EST_ROUTE_ATTR_PAT = 'legacyFormat="([^"]+)"'
EST_ROUTE_TEXT_PAT = ":routeOfFlight>([^<]+)<"
EST_ROUTE_COORD_PAT = r"\d{4}[NS]/\d{5}[EW]"
EST_ROUTE_QUERY = f"""
    WITH nullif(extract(raw_xml, '{EST_ROUTE_ATTR_PAT}'), '') AS attr_route,
         nullif(extract(raw_xml, '{EST_ROUTE_TEXT_PAT}'), '') AS text_route
    SELECT multiIf(attr_route IS NOT NULL AND match(attr_route, '{EST_ROUTE_COORD_PAT}'), attr_route,
                   text_route IS NOT NULL AND match(text_route, '{EST_ROUTE_COORD_PAT}'), text_route,
                   coalesce(attr_route, text_route)) AS route,
           toUnixTimestamp(msg_timestamp),
           (({{origin:String}} != '' AND dep_point_kind = 'airport'
             AND (upper(dep_point) = {{origin:String}}
                  OR substring({{origin:String}}, 2) = upper(dep_point)))
          + ({{dest:String}} != '' AND arr_point_kind = 'airport'
             AND (upper(arr_point) = {{dest:String}}
                  OR substring({{dest:String}}, 2) = upper(arr_point)))) AS od_matches,
           (({{origin:String}} != '' AND dep_point_kind = 'airport'
             AND NOT (upper(dep_point) = {{origin:String}}
                      OR substring({{origin:String}}, 2) = upper(dep_point)))
          + ({{dest:String}} != '' AND arr_point_kind = 'airport'
             AND NOT (upper(arr_point) = {{dest:String}}
                      OR substring({{dest:String}}, 2) = upper(arr_point)))) AS od_conflicts,
           position(route, './.') = 0 AS is_full
    FROM bronze.swim_flightdata
    WHERE upper(trimBoth(acid)) = {{callsign:String}}
      AND swim_date BETWEEN toDate(toDateTime({{start:Int64}}) - INTERVAL 30 HOUR)
                        AND toDate(toDateTime({{end:Int64}})) + 1
      AND msg_type IN ('flightPlanInformation', 'flightPlanAmendmentInformation', 'FlightRoute')
      AND msg_timestamp BETWEEN toDateTime({{start:Int64}}) - INTERVAL 30 HOUR
                            AND toDateTime({{end:Int64}})
      AND filed_departure_time BETWEEN toDateTime({{start:Int64}}) - INTERVAL 6 HOUR
                                   AND toDateTime({{end:Int64}})
      AND route != ''
      AND od_matches >= 1
      AND od_conflicts = 0
    ORDER BY od_matches DESC, is_full DESC, msg_timestamp DESC, _dedup_fp DESC
    LIMIT 1
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Strong references keep both background tasks alive for the application's lifetime.
    tasks = (asyncio.create_task(_poller()), asyncio.create_task(_est_flusher()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            # settle the cancelled coroutines; a running writer THREAD outlives this await by design —
            # the locked queue + in-thread accounting keep an overlapping tail drain correct
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # A normal recreate must not silently discard the queued tail — drain EVERY batch (each
        # cycle pops ≥1 group, success or drop, so this terminates); a dead CH stays best-effort.
        with contextlib.suppress(Exception):
            while _est_log_queue.groups:
                await _flush_once()


app = FastAPI(lifespan=lifespan)
_snapshot: dict = {"server_ts": 0.0, "aircraft": []}
# Receiver coverage polygon — batch-computed from ClickHouse history, loaded into RW; changes slowly.
_outline: list = []
# callsign → latest known route (v5.1 backstory ring) — batch-computed daily, loaded into RW.
_routes: dict = {}
# (server_ts, [[hex, lon, lat, capture_ts, alt_baro], ...]) per successful poll; in-process
# is fine now — a restart refills the full wake window in ~2 min
_track_buf: collections.deque = collections.deque(maxlen=max(1, int(HISTORY_BUFFER_S / POLL_SECONDS)))
# None = never loaded (public fails /track closed, logs once); empty frozensets = a real loaded-empty list.
# Public boot-seeds from the last-good disk cache; private never suppresses, so it never loads the set at all.
_ladd_suppress: dict | None = _read_ladd_cache(LADD_CACHE_PATH) if PUBLIC_MODE else None
_ladd_none_warned: bool = False
# Hexes _fetch dropped for the MV's is_ladd bit (public only — stays empty on private), keyed to time last
# seen; mv_track_positions carries no dbFlags, so public /track fails closed on a hex still in this TTL'd set.
_mv_ladd_hexes: dict = {}
# /flights is on-click + rarely changing (the reconciled mart is batch) — cache per hex.
_flights_cache: dict = {}
FLIGHTS_CACHE_TTL_S = float(os.environ.get("LIVEMAP_FLIGHTS_CACHE_TTL_S", "120"))
try:
    # bad env falls back instead of crashing import; floor 0 so the eviction loop always terminates
    FLIGHTS_CACHE_MAX = max(0, int(os.environ.get("LIVEMAP_FLIGHTS_CACHE_MAX", "512")))
except ValueError:
    FLIGHTS_CACHE_MAX = 512

# Workbench evidence layer (private-only) — caches keyed by the fetcher's own arg tuple, same
# eviction policy as _flights_cache. Max sizes are fixed (the design gives no per-cache env knob).
_wb_airlines_cache: dict = {}
_wb_services_cache: dict = {}
_wb_instances_cache: dict = {}
_wb_search_cache: dict = {}
WB_AIRLINES_CACHE_MAX = 64
WB_SERVICES_CACHE_MAX = 256
WB_INSTANCES_CACHE_MAX = 512
WB_SEARCH_CACHE_MAX = 256
try:
    WB_AIRLINES_CACHE_TTL_S = float(os.environ.get("LIVEMAP_WB_AIRLINES_CACHE_TTL_S", "300"))
except ValueError:
    WB_AIRLINES_CACHE_TTL_S = 300.0
try:
    WB_SERVICES_CACHE_TTL_S = float(os.environ.get("LIVEMAP_WB_SERVICES_CACHE_TTL_S", "300"))
except ValueError:
    WB_SERVICES_CACHE_TTL_S = 300.0
try:
    WB_INSTANCES_CACHE_TTL_S = float(os.environ.get("LIVEMAP_WB_INSTANCES_CACHE_TTL_S", "120"))
except ValueError:
    WB_INSTANCES_CACHE_TTL_S = 120.0
try:
    WB_SEARCH_CACHE_TTL_S = float(os.environ.get("LIVEMAP_WB_SEARCH_CACHE_TTL_S", "120"))
except ValueError:
    WB_SEARCH_CACHE_TTL_S = 120.0

# /path geometry is expensive but authorization is cheap; only geometry rides this longer cache.
_path_cache: dict = {}
PATH_CACHE_TTL_S = float(os.environ.get("LIVEMAP_PATH_CACHE_TTL_S", "900"))
try:
    PATH_CACHE_MAX = max(0, int(os.environ.get("LIVEMAP_PATH_CACHE_MAX", "256")))
except ValueError:
    PATH_CACHE_MAX = 256

_est_cache: dict = {}
EST_CACHE_TTL_S = float(os.environ.get("LIVEMAP_EST_CACHE_TTL_S", "900.0"))
try:
    EST_CACHE_MAX = max(0, int(os.environ.get("LIVEMAP_EST_CACHE_MAX", "128")))
except ValueError:
    EST_CACHE_MAX = 128
# Live-DR serving gates (design §5): snapshot membership proves nothing — the poller keeps
# serving the last good snapshot through an outage, so both bounds are checked per request.
try:
    EST_LIVE_MAX_AGE_S = max(0.0, float(os.environ.get("LIVEMAP_EST_LIVE_MAX_AGE_S", "30")))
except ValueError:
    EST_LIVE_MAX_AGE_S = 30.0
try:
    EST_LIVE_SNAP_FRESH_S = max(0.0, float(os.environ.get("LIVEMAP_EST_LIVE_SNAP_FRESH_S", "10")))
except ValueError:
    EST_LIVE_SNAP_FRESH_S = 10.0
# Cross-machine clock slack: a fix stamped slightly ahead of the host clock is fresh;
# a far-future timestamp is garbage data, not freshness — both gates reject it (rev 2).
EST_LIVE_FUTURE_SKEW_S = 2.0
# The route prior is optional work on a click-latency path: bound it well inside the click budget.
EST_ROUTE_TIMEOUT_S = 3.0
# The RW MV carries the producer's raw hex unvalidated — a malformed snapshot value must
# stay a pre-gate denial, never a computable/loggable h: subject (rev 9; ~ = readsb non-ICAO)
_LIVE_HEX_RE = re.compile(r"~?[0-9a-f]{6}")
UINT64_MAX = 2**64 - 1

# None = never loaded (callers fail closed); a failed refresh keeps last-good — the head only ever advances,
# so staleness is bounded and harmless in the historical direction.
_path_head: dict = {"expiry": 0.0, "head": None}


def _path_cache_put(fid: int, points, now: float) -> None:
    cache.put(_path_cache, fid, (now + PATH_CACHE_TTL_S, points, now), now, PATH_CACHE_MAX)


def _est_cache_put(key, payload, now: float) -> None:
    cache.put(_est_cache, key, (now + EST_CACHE_TTL_S, payload), now, EST_CACHE_MAX)


def _has_bridgeable_gap(points) -> bool:
    # the estimator's OWN eligibility is the precheck (pure, ms-scale) — an approximation here
    # kept paying the SWIM read for gaps estimate() then rejected (r2: slow/ground/no-motion)
    fixes = est.prepare(points)
    gaps = est.detect_gaps(fixes, est.DEFAULT_CONFIG)
    return any(
        isinstance(est.gap_eligibility(fixes, i, j, gaps, est.DEFAULT_CONFIG), tuple)
        for i, j in gaps
    )


async def _route_prior(points, flight):
    # Gap bridges only, and only when a gap is actually bridgeable — else never pay the SWIM read.
    # to_thread: prepare/eligibility over a 50-60k-point path is ~100 ms of CPU — never on the loop
    if not flight or not await asyncio.to_thread(_has_bridgeable_gap, points):
        return None, None
    callsign, start_s, end_s, origin_icao, dest_icao = flight
    if not (callsign or "").strip() or start_s is None or end_s is None:
        return None, None
    # no reconciled endpoint at all -> nothing to anchor the leg on; GC is the honest bridge (r4)
    if not (origin_icao or "").strip() and not (dest_icao or "").strip():
        return None, None
    try:
        # wall-clock bound (r9): the CH-side ceiling doesn't cover executor queue time under burst
        got = await asyncio.wait_for(
            asyncio.to_thread(_fetch_route, callsign, start_s, end_s, origin_icao, dest_icao),
            timeout=EST_ROUTE_TIMEOUT_S + 1.0,
        )
    except (TimeoutError, asyncio.TimeoutError):
        return None, None
    if not got:
        return None, None
    return er.parse_route_coords(got[0]) or None, got[1]


def _stamp_route_plan(result, plan_ts) -> None:
    # Stamped before the response is built: the log is a lossless record of the served meta.
    if plan_ts is None:
        return
    for segment in result.segments:
        route = segment.meta.get("route")
        if route is not None:
            route["plan_ts"] = int(plan_ts)


def _enqueue_estimate_log(rows) -> None:
    _est_log_queue.put(rows)


def _rw_rows(sql, params=None, cursor_factory=None):
    conn = psycopg2.connect(
        RW_DSN,
        connect_timeout=DB_CONNECT_TIMEOUT,
        options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )
    try:
        conn.autocommit = True
        with conn.cursor(cursor_factory=cursor_factory) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _fetch() -> dict:
    global _ladd_none_warned
    now = time.time()
    rows = _rw_rows(QUERY, cursor_factory=psycopg2.extras.RealDictCursor)
    suppress = _ladd_suppress
    if PUBLIC_MODE and suppress is None and not _ladd_none_warned:
        # Visible-once window: /aircraft leans on the MV is_ladd belt below; /track fails closed until loaded.
        print("livemap ladd suppress: not loaded yet -> /aircraft on MV belt, /track fail-closed", flush=True)
        _ladd_none_warned = True
    aircraft = []
    for r in rows:
        a = dict(r)
        ct = a["capture_ts"]
        a["capture_ts"] = ct.timestamp() if ct is not None else None
        flight = (a["flight"] or "").strip() or None
        a["flight"] = flight
        # pop in BOTH modes so the flag never rides the payload; .get/.pop tolerate a partial row (test doubles).
        mv_is_ladd = a.pop("is_ladd", None)
        hex_ = a.get("hex")
        if PUBLIC_MODE:
            # LADD: drop currently-listed airframes before they reach any client (belt to the mart's flag).
            if mv_is_ladd:
                # record the belt-suppressed hex so /track (dbFlags-blind) can also fail closed for it
                h = (hex_ or "").strip().lower()
                if h:
                    _mv_ladd_hexes[h] = now
            if _is_ladd_suppressed(hex_, flight, mv_is_ladd=mv_is_ladd, suppress=suppress):
                continue
        a["route"] = _routes.get(flight)
        # jsonb arrives as JSON text over pgwire — coerce to a list (or None); never raise
        nm = a.get("nav_modes")
        if isinstance(nm, str):
            try:
                nm = json.loads(nm)
            except (ValueError, TypeError):
                nm = None
        a["nav_modes"] = nm if isinstance(nm, list) else None
        aircraft.append(a)
    # drop belt entries not re-seen within the TTL so /track stops suppressing a hex that has gone quiet
    for h in [h for h, ts in _mv_ladd_hexes.items() if now - ts > HISTORY_BUFFER_S]:
        del _mv_ladd_hexes[h]
    return {"server_ts": now, "aircraft": aircraft}


def _fetch_outline() -> list:
    # latest complete generation only (max gen) — never a half-written polygon; table may not exist yet
    rows = _rw_rows(
        "SELECT lon, lat FROM range_outline "
        "WHERE gen = (SELECT max(gen) FROM range_outline) ORDER BY bin"
    )
    ring = [[float(lon), float(lat)] for lon, lat in rows]
    if ring:
        ring.append(ring[0])  # close the polygon
    return ring


def _fetch_routes() -> dict:
    # latest complete generation only (max gen); table may not exist yet
    rows = _rw_rows(
        "SELECT callsign, origin_code, origin_city, dest_code, dest_city, departed_epoch "
        "FROM dim_flight_routes "
        "WHERE gen = (SELECT max(gen) FROM dim_flight_routes)"
    )
    return {
        cs: {
            "origin": oc, "origin_city": ocity,
            "dest": dc, "dest_city": dcity,
            "departed_epoch": dep,
        }
        for cs, oc, ocity, dc, dcity, dep in rows
    }


def _fetch_track(icao: str) -> list:
    rows = _rw_rows(TRACK_QUERY, params=(icao,))
    return [[lon, lat, ct.timestamp(), alt] for lon, lat, ct, alt in rows]


def _ch_client():
    # lazy import: a missing clickhouse-connect degrades /flights to [], never crashes the sidecar
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, username=CH_USER, password=CH_PASSWORD, database=CH_DB,
        connect_timeout=3, send_receive_timeout=CH_QUERY_TIMEOUT_S,
        settings={"max_execution_time": CH_QUERY_TIMEOUT_S},
    )


def _ch_writer_client():
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, username=CH_WRITER_USER, password=CH_WRITER_PASSWORD,
        database="bronze", connect_timeout=3, send_receive_timeout=CH_QUERY_TIMEOUT_S,
        settings={"max_execution_time": CH_QUERY_TIMEOUT_S},
    )


def _fetch_flights(hex_: str) -> list:
    client = _ch_client()
    try:
        res = client.query(FLIGHTS_QUERY, parameters={"hex": hex_.lower()})
    finally:
        client.close()
    out = []
    for src, ts, o_code, o_name, d_code, d_name, callsign, flight_id in res.result_rows:
        out.append({
            "src": src,
            # CH driver returns naive UTC datetimes — pin tzinfo so process TZ can't skew epochs
            "ts": ts.replace(tzinfo=datetime.timezone.utc).timestamp() if ts is not None else None,
            "origin": {"code": o_code, "name": o_name},
            "dest": {"code": d_code, "name": d_name},
            "callsign": (callsign or "").strip() or None,
            # decimal string, not a number: cityHash64 UInt64 overflows JS Number, so it must stay text end-to-end
            "flight_id": flight_id,
        })
    return out


def _valid_flight_id(fid: str) -> bool:
    # len cap MUST be first — it keeps int() under CPython's 4300-digit str->int limit (which raises ValueError,
    # and this runs outside the endpoint's try/except). UInt64 max is 20 digits.
    return len(fid) <= 20 and fid.isascii() and fid.isdigit() and int(fid) <= UINT64_MAX


def _fetch_path_rich(flight_id: str) -> list:
    client = _ch_client()
    try:
        res = client.query(PATH_QUERY, parameters={"fid": int(flight_id)})
    finally:
        client.close()
    return [
        (int(ts), lat, lon, alt, int(og or 0), gs, trk, src)
        for ts, lat, lon, alt, og, gs, trk, src in res.result_rows
    ]


def _lean_points(rich) -> list:
    # the frozen /path wire projects off the rich row — one loader, two consumers (design §4)
    return [[lon, lat, ts, alt, src] for ts, lat, lon, alt, _og, _gs, _trk, src in rich]


def _fetch_path_auth(flight_id: str):
    client = _ch_client()
    try:
        res = client.query(PATH_AUTH_QUERY, parameters={"fid": int(flight_id)})
    finally:
        client.close()
    if not res.result_rows:
        return None
    icao24, callsign, is_ladd, start_s, end_s, start_day = res.result_rows[0]
    return icao24, callsign, bool(is_ladd), start_s, end_s, start_day


def _fetch_od(fid: int):
    client = _ch_client()
    try:
        res = client.query(PATH_OD_QUERY, parameters={"fid": fid})
    finally:
        client.close()
    if not res.result_rows:
        return est.OD(), None
    (olat, olon, osrc, oagr, dlat, dlon, dsrc, dagr,
     callsign, start_s, end_s, origin_icao, dest_icao) = res.result_rows[0]
    origin = est.Endpoint(olat, olon, osrc, oagr) if olat is not None and olon is not None else est.Endpoint()
    dest = est.Endpoint(dlat, dlon, dsrc, dagr) if dlat is not None and dlon is not None else est.Endpoint()
    return est.OD(origin=origin, dest=dest), (callsign, start_s, end_s, origin_icao, dest_icao)


def _fetch_route(callsign, start_time, end_time, origin_icao, dest_icao):
    # Any failure — no plan, malformed row, CH down, execution timeout — degrades the bridge to pure
    # GC: the filed route is a prior, never a serving dependency.
    try:
        client = _ch_client()
        try:
            res = client.query(
                EST_ROUTE_QUERY,
                parameters={
                    "callsign": (callsign or "").strip().upper(),
                    "start": int(start_time),
                    "end": int(end_time),
                    "origin": (origin_icao or "").strip().upper(),
                    "dest": (dest_icao or "").strip().upper(),
                },
                settings={"max_execution_time": EST_ROUTE_TIMEOUT_S},
            )
        finally:
            client.close()
        if not res.result_rows:
            return None
        route, plan_ts = res.result_rows[0][:2]
        return (route, plan_ts) if route else None
    except Exception as exc:
        # once per exception type: a broken grant or a timing tail must not flood stderr per click
        if type(exc).__name__ not in _est_route_fetch_warned:
            _est_route_fetch_warned.add(type(exc).__name__)
            print(f"livemap estimate route fetch skipped: {type(exc).__name__}: {exc}", flush=True)
        return None


# The sibling logic is config-free — these bind this module's env-derived settings and CH client
# factory to it, resolved per call so a rebound factory or path takes effect immediately.
def _fetch_path_head():
    return pf.fetch_head(_ch_client)


async def _get_path_head(now):
    return await pf.get_head(now, _path_head, _fetch_path_head)


def _fetch_provisional(fid, icao24, start_s, end_s):
    return pf.fetch_provisional(_ch_client, fid, icao24, start_s, end_s)


def _should_refresh_ladd(state, tick) -> bool:
    return ladd.should_refresh(state, tick, LADD_REFRESH_TICKS)


def _track_belt_suppressed(hex_, now, mv_ladd_hexes) -> bool:
    return ladd.track_belt_suppressed(hex_, now, mv_ladd_hexes, HISTORY_BUFFER_S)


def _is_unknown_table_error(exc) -> bool:
    # clickhouse-connect sets code/name on DatabaseError; code 60 / UNKNOWN_TABLE = a missing relation. Prefer the
    # structured code, then the symbolic name, then fall back to the server text (Code: 60 / UNKNOWN_TABLE, and
    # the pre-structured "doesn't exist" wording so an older/plain error still resolves).
    code = getattr(exc, "code", None)
    if code is not None and str(code) == "60":
        return True
    if str(getattr(exc, "name", "") or "").upper() == "UNKNOWN_TABLE":
        return True
    s = str(exc).lower()
    return "code: 60" in s or "unknown_table" in s or "unknown table" in s or "doesn't exist" in s


# Workbench thin fetchers (private-only): SQL text + row shaping live in wb; this layer only owns
# the CH round trip and the tier-mart-absent degradation (query, and on unknown-table, requery).
def _fetch_wb_airlines(q, limit, offset) -> dict:
    params = {"q": (q or "").strip(), "limit": limit, "offset": offset}
    client = _ch_client()
    try:
        try:
            rows = client.query(wb.AIRLINES_QUERY_TIER, parameters=params).result_rows
            with_tier = True
        except Exception as exc:
            if not _is_unknown_table_error(exc):
                raise
            rows = client.query(wb.AIRLINES_QUERY_NO_TIER, parameters=params).result_rows
            with_tier = False
        total = client.query(wb.AIRLINES_COUNT_QUERY, parameters={"q": params["q"]}).result_rows[0][0]
    finally:
        client.close()
    return {"airlines": [wb.shape_airline_row(r, with_tier) for r in rows],
            "total": total, "limit": limit, "offset": offset}


def _fetch_wb_services(airline, q, limit, offset) -> dict:
    params = {"airline": (airline or "").strip(), "q": (q or "").strip().upper(),
              "limit": limit, "offset": offset}
    client = _ch_client()
    try:
        try:
            rows = client.query(wb.SERVICES_QUERY_TIER, parameters=params).result_rows
            with_tier = True
        except Exception as exc:
            if not _is_unknown_table_error(exc):
                raise
            rows = client.query(wb.SERVICES_QUERY_NO_TIER, parameters=params).result_rows
            with_tier = False
        total = client.query(
            wb.SERVICES_COUNT_QUERY, parameters={"airline": params["airline"], "q": params["q"]}
        ).result_rows[0][0]
        callsigns = [r[0] for r in rows]
        top_od_rows = (
            client.query(wb.SERVICES_TOP_OD_QUERY, parameters={"callsigns": callsigns}).result_rows
            if callsigns else []
        )
    finally:
        client.close()
    top_od = wb.group_top_od(top_od_rows)
    return {"services": [wb.shape_service_row(r, with_tier, top_od) for r in rows],
            "total": total, "limit": limit, "offset": offset}


def _fetch_wb_instances(callsign, airline, hex_, reg, airport, od, type_, military,
                         day_from, day_to, sort, limit, offset) -> dict:
    params = wb.instances_params(callsign, airline, hex_, reg, airport, od, type_, military,
                                  day_from, day_to)
    params["limit"] = limit
    params["offset"] = offset
    main_tier = wb.INSTANCES_QUERY_TIER_ASC if sort == "day_asc" else wb.INSTANCES_QUERY_TIER_DESC
    main_no_tier = wb.INSTANCES_QUERY_NO_TIER_ASC if sort == "day_asc" else wb.INSTANCES_QUERY_NO_TIER_DESC
    client = _ch_client()
    try:
        try:
            rows = client.query(main_tier, parameters=params,
                                settings=wb.INSTANCES_QUERY_SETTINGS).result_rows
            total = client.query(wb.INSTANCES_COUNT_QUERY_TIER, parameters=params).result_rows[0][0]
            od_rows = client.query(wb.INSTANCES_OD_BREAKDOWN_QUERY_TIER, parameters=params).result_rows
        except Exception as exc:
            if not _is_unknown_table_error(exc):
                raise
            # military filtering has no meaning without the tier mart — an honest empty, not a silent no-op
            if params["military"]:
                return {"instances": [], "od_breakdown": [], "total": 0, "limit": limit, "offset": offset,
                        "military_filter_available": False}
            rows = client.query(main_no_tier, parameters=params,
                                settings=wb.INSTANCES_QUERY_SETTINGS).result_rows
            total = client.query(wb.INSTANCES_COUNT_QUERY_NO_TIER, parameters=params).result_rows[0][0]
            od_rows = client.query(wb.INSTANCES_OD_BREAKDOWN_QUERY_NO_TIER, parameters=params).result_rows
    finally:
        client.close()
    return {"instances": [wb.shape_instance_row(r) for r in rows],
            "od_breakdown": wb.shape_od_breakdown(od_rows),
            "total": total, "limit": limit, "offset": offset}


def _fetch_wb_search(q, limit) -> dict:
    params = wb.search_params(q)
    params["limit"] = limit
    client = _ch_client()
    try:
        airlines = client.query(wb.SEARCH_AIRLINES_QUERY, parameters=params).result_rows
        services = client.query(wb.SEARCH_SERVICES_QUERY, parameters=params).result_rows
        airframes = client.query(wb.SEARCH_AIRFRAMES_QUERY, parameters=params).result_rows
        airports = client.query(wb.SEARCH_AIRPORTS_QUERY, parameters=params).result_rows
    finally:
        client.close()
    return {
        "airlines": [wb.shape_search_airline(r) for r in airlines],
        "services": [wb.shape_search_service(r) for r in services],
        "airframes": [wb.shape_search_airframe(r) for r in airframes],
        "airports": [wb.shape_search_airport(r) for r in airports],
    }


async def _flush_once() -> None:
    queue = _est_log_queue

    def insert_rows():
        # drain + accounting live IN the thread (§4): a shutdown cancel abandons the await, never the
        # thread — and a pre-start cancel leaves the rows queued for the lifespan tail drain
        global _est_missing_table_warned
        rows, ngroups = queue.drain(EST_FLUSH_MAX_ROWS)
        if not rows:
            return
        try:
            client = _ch_writer_client()
            try:
                client.insert(
                    "path_estimates",
                    rows,
                    column_names=ess.INSERT_COLUMNS,
                    column_type_names=ess.INSERT_TYPES,
                )
            finally:
                # a close failure after a successful INSERT is not data loss — never count it as one
                with contextlib.suppress(Exception):
                    client.close()
            queue.record_written(ngroups)
        except Exception as exc:
            queue.record_drop(ngroups)
            if _is_unknown_table_error(exc):
                if not _est_missing_table_warned:
                    print(f"livemap estimate log table absent; dropping estimates: {exc}", flush=True)
                    _est_missing_table_warned = True
            else:
                print(
                    f"livemap estimate log flush dropped {ngroups} estimates: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

    await asyncio.to_thread(insert_rows)


async def _est_flusher() -> None:
    while True:
        await asyncio.sleep(EST_FLUSH_S)
        await _flush_once()


def _ladd_missing_table(exc) -> bool:
    return ladd.missing_table(exc, _is_unknown_table_error)


def _fetch_ladd_suppress() -> dict:
    return ladd.fetch_suppress(_ch_client)


def _refresh_ladd_suppress(current):
    return ladd.refresh_suppress(current, _fetch_ladd_suppress, LADD_CACHE_PATH, _ladd_missing_table)


async def _poller() -> None:
    global _snapshot, _outline, _routes, _ladd_suppress
    n = 0
    while True:
        try:
            # refresh the suppression set before _fetch so even the first snapshot is already filtered;
            # private suppresses nothing, so it never reads dim_ladd nor the disk cache
            if PUBLIC_MODE and _should_refresh_ladd(_ladd_suppress, n):
                _ladd_suppress = await asyncio.to_thread(_refresh_ladd_suppress, _ladd_suppress)
            # psycopg2 is sync; offload so the ~1s query never blocks the event loop
            _snapshot = await asyncio.to_thread(_fetch)
            _track_buf.append((
                _snapshot["server_ts"],
                [[a["hex"], a["lon"], a["lat"], a["capture_ts"], a["alt_baro"]]
                 for a in _snapshot["aircraft"]],
            ))
            # outline + routes change slowly — refresh on first tick, then every ~5 min
            if n % SLOW_REFRESH_TICKS == 0:
                try:
                    _outline = await asyncio.to_thread(_fetch_outline)
                except Exception as exc:
                    print(f"livemap outline refresh skipped: {exc}", flush=True)
                try:
                    _routes = await asyncio.to_thread(_fetch_routes)
                except Exception as exc:
                    print(f"livemap routes refresh skipped: {exc}", flush=True)
        except Exception as exc:  # keep serving the last good snapshot on a blip
            print(f"livemap poll error: {exc}", flush=True)
        n += 1
        await asyncio.sleep(POLL_SECONDS)


if PUBLIC_MODE:
    # Public hardening: per-IP rate limit on the DB endpoints, an edge-cache hint on /aircraft, and security
    # headers. Registered only in public mode so the private instance's responses stay byte-identical.
    @app.middleware("http")
    async def _public_hardening(request, call_next):
        path = request.url.path
        if path.startswith(RATE_LIMITED_PREFIXES) and not rl.allow_request(
            # monotonic, not wall-clock: an NTP step backwards would make elapsed negative and over-deny
            _client_ip(request), time.monotonic()
        ):
            # Generic body: the limit is rate-based, not identity-based, so a 429 can't be a privacy oracle.
            resp = JSONResponse({"detail": "rate limited"}, status_code=429)
        else:
            resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        if path == "/aircraft":
            # s-maxage=1: with a dashboard Cache Rule making /aircraft cache-eligible (Cloudflare doesn't
            # cache JSON by default), the edge absorbs the snapshot fan-out; browsers still poll ~live.
            resp.headers["Cache-Control"] = "public, s-maxage=1"
        elif path.startswith(("/path/", "/estimate/live/")):
            # rate-limit 429s bypass the endpoint's envelope — keep the every-response no-store belt intact
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp


def _ring_centroid(ring):
    # area centroid on a local equirectangular plane (ring is Kanto-scale, no wrap);
    # a vertex mean would drift with per-bearing bin density
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else list(ring)
    if len(pts) < 3:
        return None
    lat0 = sum(p[1] for p in pts) / len(pts)
    k = math.cos(math.radians(lat0))
    xy = [(p[0] * k, p[1]) for p in pts]
    a2 = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1], strict=True):
        cross = x1 * y2 - x2 * y1
        a2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(a2) < 1e-9:
        return None
    return [cx / (3 * a2) / k, cy / (3 * a2)]


async def _load_path_input(flight_id: str) -> dict:
    # Suppressed / unknown / missing-table / CH-down all return the same empty shape — never 404/500, no
    # privacy oracle; "provisional" rides every response so the flag itself leaks nothing.
    def result(status, points, auth, as_of):
        return {"status": status, "points": points, "auth": auth, "as_of": as_of}

    if not _valid_flight_id(flight_id):
        return result("denied", [], None, time.time())
    suppress = _ladd_suppress
    # Public-only obligation: private serves every airframe, so it never fails closed on an unloaded set.
    if PUBLIC_MODE and suppress is None:
        return result("denied", [], None, time.time())
    try:
        auth = await asyncio.to_thread(_fetch_path_auth, flight_id)
    except Exception as exc:
        print(f"livemap path auth failed: {type(exc).__name__}: {exc}", flush=True)
        return result("denied", [], None, time.time())
    if auth is None:
        return result("denied", [], None, time.time())
    icao24, callsign, mart_ladd, start_s, end_s, start_day = auth
    if PUBLIC_MODE and _is_ladd_suppressed(icao24, callsign, mv_is_ladd=mart_ladd, suppress=suppress):
        return result("denied", [], auth, time.time())
    fid = int(flight_id)  # canonical cache key: leading-zero aliases must not mint distinct entries
    now = time.time()
    hit = _path_cache.get(fid)
    valid_hit = bool(hit) and hit[0] > now
    cached_empty = valid_hit and not hit[1]
    input_as_of = hit[2] if valid_hit else now
    if valid_hit and hit[1]:
        return result("settled", hit[1], auth, input_as_of)
    if not cached_empty:
        try:
            points = await asyncio.to_thread(_fetch_path_rich, flight_id)
        except Exception as exc:
            print(f"livemap path fetch failed: {type(exc).__name__}: {exc}", flush=True)
            return result("denied", [], auth, now)
        # §7 audit semantics: input_as_of is READ-COMPLETION time, not branch entry — sample after the fetch
        input_as_of = time.time()
        if points:
            _path_cache_put(fid, points, input_as_of)  # immutable settled-day history: cache non-empty successes
            return result("settled", points, auth, input_as_of)
    # settled empty → eligibility gate: an empty cache HIT classifies-and-continues, it never short-circuits
    head = await _get_path_head(now)
    if head is None or start_day is None:  # cold head fetch failed / windowless spine row → fail closed
        return result("denied", [], auth, now)
    if start_day <= head:
        # historical pathless keeps today's behavior; the empty is cacheable only now that it's classified
        if not cached_empty:
            _path_cache_put(fid, [], input_as_of)
        return result("settled_empty", [], auth, input_as_of)
    if start_s is None or end_s is None:
        return result("denied", [], auth, now)
    try:
        points = await asyncio.to_thread(_fetch_provisional, fid, icao24, start_s, end_s)
    except Exception as exc:
        print(f"livemap provisional path failed: {type(exc).__name__}: {exc}", flush=True)
        return result("denied", [], auth, now)
    # bypasses _path_cache: bronze grows all day and the flight settles within days — a 900 s entry is
    # wrong in both directions and the recompute is cheap (~114 ms worst-case measured)
    return result("provisional", points, auth, time.time())


class _Ctx:
    # Late binding: the router modules resolve every name here at call time, so a test (or a
    # runtime rebind) that patches this module's attributes still steers the handlers.
    def __init__(self, g):
        object.__setattr__(self, "_g", g)

    def __getattr__(self, name):
        try:
            return self._g[name]
        except KeyError:
            raise AttributeError(name) from None


_ctx = _Ctx(globals())
app.include_router(routes_live.build_router(_ctx))
app.include_router(routes_aircraft.build_router(_ctx))
app.include_router(routes_path.build_router(_ctx))

if not PUBLIC_MODE:
    wb = _load_sibling("workbench")
    routes_workbench = _load_sibling("routes_workbench")
    app.include_router(routes_workbench.build_router(_ctx))


# Header-less statics get heuristic-cached by browsers — a stale map.js once outlived its index.html
class RevalidatedStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class PublicStatic(RevalidatedStatic):
    # The privacy boundary lives in the filesystem layer: get_path has already collapsed // and ..
    # by the time this runs, and StaticFiles serves GET and HEAD alike — no per-route arm to miss.
    async def get_response(self, path: str, scope):
        if path == "features" or path.startswith("features/"):
            raise StarletteHTTPException(status_code=404)
        return await super().get_response(path, scope)


# Mounted last so /aircraft and /healthz win; serves index.html at /
app.mount(
    "/",
    (PublicStatic if PUBLIC_MODE else RevalidatedStatic)(
        directory=os.path.join(os.path.dirname(__file__), "static"), html=True),
    name="static",
)
