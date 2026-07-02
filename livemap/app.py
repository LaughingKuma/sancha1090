import asyncio
import collections
import contextlib
import datetime
import json
import os
import time
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

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

# Real antenna is a secret (home rooftop) — from .env; default is the public Carrot Tower landmark.
FEEDER_LAT = float(os.environ.get("LIVEMAP_FEEDER_LAT", "35.6434"))
FEEDER_LON = float(os.environ.get("LIVEMAP_FEEDER_LON", "139.6692"))

# recv rides the payload end-to-end (the P2 multi-receiver seam); rendered uniformly today.
QUERY = """
    SELECT capture_ts, hex, flight, lat, lon, alt_baro, gs, track,
           typecode, aircraft_desc, registration, body_class, is_military, is_helicopter,
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

# "Where else has it been": clean fact_flights history + rooftop legs fresher than the OpenSky
# watermark fill the ~48h arrival lag. UNION wrapped in a subquery so ORDER BY/LIMIT bind the whole set.
FLIGHTS_QUERY = f"""
    WITH (SELECT coalesce(max(last_seen), toDateTime64('1970-01-01 00:00:00', 6))
          FROM {CH_DB}.fact_flights WHERE icao24 = {{hex:String}}) AS wm
    SELECT * FROM (
        SELECT 'opensky' AS src, last_seen AS ts,
               coalesce(origin_iata, origin_icao) AS o_code, coalesce(origin_city, origin_name) AS o_name,
               coalesce(dest_iata,   dest_icao)   AS d_code, coalesce(dest_city,   dest_name)   AS d_name, callsign
        FROM {CH_DB}.fact_flights
        WHERE icao24 = {{hex:String}} AND (origin_icao IS NOT NULL OR dest_icao IS NOT NULL)
        UNION ALL
        SELECT 'rooftop' AS src, end_time AS ts,
               origin_icao AS o_code, origin_name AS o_name,
               dest_icao   AS d_code, dest_name   AS d_name, callsign
        FROM {CH_DB}.fct_flight_legs
        WHERE icao24 = {{hex:String}} AND end_time > wm
          AND (origin_icao IS NOT NULL OR dest_icao IS NOT NULL)
    ) ORDER BY ts DESC LIMIT 10
"""

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # hold a reference so the poller task isn't garbage-collected mid-flight
    task = asyncio.create_task(_poller())
    try:
        yield
    finally:
        task.cancel()
        # await the cancellation so shutdown doesn't leave it pending mid-_fetch
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan)
_snapshot: dict = {"server_ts": 0.0, "aircraft": []}
# Receiver coverage polygon — batch-computed from ClickHouse history, loaded into RW; changes slowly.
_outline: list = []
# callsign → latest known route (v5.1 backstory ring) — batch-computed daily, loaded into RW.
_routes: dict = {}
# (server_ts, [[hex, lon, lat, capture_ts, alt_baro], ...]) per successful poll; in-process
# is fine now — a restart refills the full wake window in ~2 min
_track_buf: collections.deque = collections.deque(maxlen=max(1, int(HISTORY_BUFFER_S / POLL_SECONDS)))
# /flights is on-click + rarely changing (fact_flights is batch, rooftop legs ~hourly) — cache per hex.
_flights_cache: dict = {}
FLIGHTS_CACHE_TTL_S = float(os.environ.get("LIVEMAP_FLIGHTS_CACHE_TTL_S", "120"))
try:
    # bad env falls back instead of crashing import; floor 0 so the eviction loop always terminates
    FLIGHTS_CACHE_MAX = max(0, int(os.environ.get("LIVEMAP_FLIGHTS_CACHE_MAX", "512")))
except ValueError:
    FLIGHTS_CACHE_MAX = 512


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
    rows = _rw_rows(QUERY, cursor_factory=psycopg2.extras.RealDictCursor)
    aircraft = []
    for r in rows:
        a = dict(r)
        ct = a["capture_ts"]
        a["capture_ts"] = ct.timestamp() if ct is not None else None
        flight = (a["flight"] or "").strip() or None
        a["flight"] = flight
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
    return {"server_ts": time.time(), "aircraft": aircraft}


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


def _fetch_flights(hex_: str) -> list:
    client = _ch_client()
    try:
        res = client.query(FLIGHTS_QUERY, parameters={"hex": hex_.lower()})
    finally:
        client.close()
    out = []
    for src, ts, o_code, o_name, d_code, d_name, callsign in res.result_rows:
        out.append({
            "src": src,
            # CH driver returns naive UTC datetimes — pin tzinfo so process TZ can't skew epochs
            "ts": ts.replace(tzinfo=datetime.timezone.utc).timestamp() if ts is not None else None,
            "origin": {"code": o_code, "name": o_name},
            "dest": {"code": d_code, "name": d_name},
            "callsign": (callsign or "").strip() or None,
        })
    return out


async def _poller() -> None:
    global _snapshot, _outline, _routes
    n = 0
    while True:
        try:
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


@app.get("/aircraft")
async def aircraft() -> JSONResponse:
    return JSONResponse(_snapshot)


@app.get("/range-outline")
async def range_outline() -> JSONResponse:
    # center = receiver (Carrot Tower default in public; real antenna from .env); ring = coverage polygon
    return JSONResponse({"center": [FEEDER_LON, FEEDER_LAT], "ring": _outline})


@app.get("/track/{icao}")
async def track(icao: str) -> JSONResponse:
    # psycopg2 is sync; offload like the poller. Clicks are rare — a per-click query is cheap.
    try:
        points = await asyncio.to_thread(_fetch_track, icao)
    except Exception as exc:  # RW down → empty track; selection and wake still render
        print(f"livemap track fetch failed: {exc}", flush=True)
        points = []
    # unknown hex → empty points, 200: absence is a normal state, never a 404
    return JSONResponse({"hex": icao, "points": points})


@app.get("/flights/{hex}")
async def flights(hex: str) -> JSONResponse:
    # recent origin→dest history from CH; on-click like /track, never polled. CH down → empty, never 500.
    key = hex.lower()
    now = time.time()
    hit = _flights_cache.get(key)
    if hit and hit[0] > now:
        return JSONResponse({"hex": hex, "flights": hit[1]})
    try:
        rows = await asyncio.to_thread(_fetch_flights, key)
        _flights_cache[key] = (now + FLIGHTS_CACHE_TTL_S, rows)  # cache only successes
        if len(_flights_cache) > FLIGHTS_CACHE_MAX:
            # entries only age out on same-key hits, so a many-hex sweep would grow the dict unboundedly
            for k in [k for k, v in _flights_cache.items() if v[0] <= now]:
                del _flights_cache[k]
            while len(_flights_cache) > FLIGHTS_CACHE_MAX:
                del _flights_cache[min(_flights_cache, key=lambda k: _flights_cache[k][0])]
    except Exception as exc:
        # type name distinguishes a real CH outage from a bug the broad never-500 catch would mask
        print(f"livemap flights fetch failed: {type(exc).__name__}: {exc}", flush=True)
        rows = []
    return JSONResponse({"hex": hex, "flights": rows})


@app.get("/history")
async def history(s: float = 90.0) -> JSONResponse:
    # clamp to the wake buffer — backfill serves the 90 s wake, never the full 30 min ring
    s = min(s if s > 0 else 90.0, HISTORY_BUFFER_S)
    cutoff = time.time() - s
    snaps = [[ts, rows] for ts, rows in _track_buf if ts >= cutoff]
    return JSONResponse({"snapshots": snaps})


@app.get("/healthz")
async def healthz() -> JSONResponse:
    fresh = (time.time() - _snapshot["server_ts"]) < 10
    return JSONResponse(
        {"ok": fresh, "count": len(_snapshot["aircraft"]), "server_ts": _snapshot["server_ts"]},
        status_code=200 if fresh else 503,
    )


# Header-less statics get heuristic-cached by browsers — a stale map.js once outlived its index.html
class RevalidatedStatic(StaticFiles):
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Mounted last so /aircraft and /healthz win; serves index.html at /
app.mount(
    "/",
    RevalidatedStatic(directory=os.path.join(os.path.dirname(__file__), "static"), html=True),
    name="static",
)
