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
# 30 min of track history regardless of poll cadence (compose runs 0.5 s ticks)
TRACK_BUFFER_S = 1800
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
           airline_name, reg_country, recv
    FROM mv_current_aircraft
    WHERE lat IS NOT NULL AND lon IS NOT NULL
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
# by design — lost on restart, refills at poll cadence
_track_buf: collections.deque = collections.deque(maxlen=max(1, int(TRACK_BUFFER_S / POLL_SECONDS)))


def _fetch() -> dict:
    conn = psycopg2.connect(
        RW_DSN,
        connect_timeout=DB_CONNECT_TIMEOUT,
        options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )
    try:
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(QUERY)
            rows = cur.fetchall()
    finally:
        conn.close()
    aircraft = []
    for r in rows:
        ct = r["capture_ts"]
        flight = (r["flight"] or "").strip() or None
        aircraft.append(
            {
                "capture_ts": ct.timestamp() if ct is not None else None,
                "hex": r["hex"],
                "flight": flight,
                "route": _routes.get(flight),
                "lat": r["lat"],
                "lon": r["lon"],
                "alt_baro": r["alt_baro"],
                "gs": r["gs"],
                "track": r["track"],
                "typecode": r["typecode"],
                "aircraft_desc": r["aircraft_desc"],
                "registration": r["registration"],
                "body_class": r["body_class"],
                "is_military": r["is_military"],
                "is_helicopter": r["is_helicopter"],
                "airline_name": r["airline_name"],
                "reg_country": r["reg_country"],
                "recv": r["recv"],
            }
        )
    return {"server_ts": time.time(), "aircraft": aircraft}


def _fetch_outline() -> list:
    conn = psycopg2.connect(
        RW_DSN,
        connect_timeout=DB_CONNECT_TIMEOUT,
        options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            # latest complete generation only (max gen) — never a half-written polygon; table may not exist yet
            cur.execute(
                "SELECT lon, lat FROM range_outline "
                "WHERE gen = (SELECT max(gen) FROM range_outline) ORDER BY bin"
            )
            ring = [[float(lon), float(lat)] for lon, lat in cur.fetchall()]
    finally:
        conn.close()
    if ring:
        ring.append(ring[0])  # close the polygon
    return ring


def _fetch_routes() -> dict:
    conn = psycopg2.connect(
        RW_DSN,
        connect_timeout=DB_CONNECT_TIMEOUT,
        options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            # latest complete generation only (max gen); table may not exist yet
            cur.execute(
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
                for cs, oc, ocity, dc, dcity, dep in cur.fetchall()
            }
    finally:
        conn.close()


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
    points = []
    last_ts = None
    # no lock: appends happen on this same event loop, so iteration never races a mutation
    for _, rows in _track_buf:
        for row in rows:
            if row[0] != icao:
                continue
            if row[3] != last_ts:  # frozen rows repeat capture_ts across polls
                points.append(row[1:])
                last_ts = row[3]
            break
    # unknown hex → empty points, 200: absence is a normal state, never a 404
    return JSONResponse({"hex": icao, "points": points})


@app.get("/history")
async def history(s: float = 90.0) -> JSONResponse:
    # clamp to (0, 120] — backfill serves the 90 s wake, never the full 30 min ring
    s = min(s if s > 0 else 90.0, 120.0)
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


# Mounted last so /aircraft and /healthz win; serves index.html at /
app.mount("/", StaticFiles(directory="static", html=True), name="static")
