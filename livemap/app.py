import asyncio
import collections
import contextlib
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

# Real antenna is a secret (home rooftop) — from .env; default is the public Carrot Tower landmark.
FEEDER_LAT = float(os.environ.get("LIVEMAP_FEEDER_LAT", "35.6434"))
FEEDER_LON = float(os.environ.get("LIVEMAP_FEEDER_LON", "139.6692"))

# recv rides the payload end-to-end (the P2 multi-receiver seam); rendered uniformly today.
QUERY = """
    SELECT capture_ts, hex, flight, lat, lon, alt_baro, gs, track,
           typecode, aircraft_desc, registration, body_class, is_military, is_helicopter,
           airline_name, reg_country, recv, own_op, year, category,
           squawk, position_source
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
# Receiver coverage polygon — batch-computed from Trino history, loaded into RW; changes slowly.
_outline: list = []
# callsign → latest known route (v5.1 backstory ring) — batch-computed daily, loaded into RW.
_routes: dict = {}
# (server_ts, [[hex, lon, lat, capture_ts, alt_baro], ...]) per successful poll; in-process
# is fine now — a restart refills the full wake window in ~2 min
_track_buf: collections.deque = collections.deque(maxlen=max(1, int(HISTORY_BUFFER_S / POLL_SECONDS)))


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
