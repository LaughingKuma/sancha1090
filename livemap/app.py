import asyncio
import contextlib
import os
import time
from contextlib import asynccontextmanager

import psycopg2
import psycopg2.extras
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Server-side cache is the whole point: N browser tabs share ONE RW query/s, never N.
POLL_SECONDS = float(os.environ.get("LIVEMAP_POLL_SECONDS", "1.0"))
RW_DSN = os.environ.get(
    "LIVEMAP_RW_DSN", "postgresql://root@risingwave:4566/dev"
)
# Bound the connect + query so a stalled RW can't freeze the poller and stale /healthz forever
DB_CONNECT_TIMEOUT = int(os.environ.get("LIVEMAP_DB_CONNECT_TIMEOUT", "3"))
DB_STATEMENT_TIMEOUT_MS = int(os.environ.get("LIVEMAP_DB_STATEMENT_TIMEOUT_MS", "3000"))

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
        aircraft.append(
            {
                "capture_ts": ct.timestamp() if ct is not None else None,
                "hex": r["hex"],
                "flight": (r["flight"] or "").strip() or None,
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


async def _poller() -> None:
    global _snapshot
    while True:
        try:
            # psycopg2 is sync; offload so the ~1s query never blocks the event loop
            _snapshot = await asyncio.to_thread(_fetch)
        except Exception as exc:  # keep serving the last good snapshot on a blip
            print(f"livemap poll error: {exc}", flush=True)
        await asyncio.sleep(POLL_SECONDS)


@app.get("/aircraft")
async def aircraft() -> JSONResponse:
    return JSONResponse(_snapshot)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    fresh = (time.time() - _snapshot["server_ts"]) < 10
    return JSONResponse(
        {"ok": fresh, "count": len(_snapshot["aircraft"]), "server_ts": _snapshot["server_ts"]},
        status_code=200 if fresh else 503,
    )


# Mounted last so /aircraft and /healthz win; serves index.html at /
app.mount("/", StaticFiles(directory="static", html=True), name="static")
