import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def build_router(ctx) -> APIRouter:
    router = APIRouter()

    @router.get("/track/{icao}")
    async def track(icao: str) -> JSONResponse:
        # LADD is a public-instance obligation (ruling 2026-07-29): the whole gate — fail-closed included —
        # is skipped on private, which suppresses nothing and so never has a loaded set to check.
        if ctx.PUBLIC_MODE:
            # None state = suppression never loaded → fail closed for ALL hexes (no MV belt of its own here).
            if ctx._ladd_suppress is None:
                return JSONResponse({"hex": icao, "points": []})
            # A currently-listed hex returns empty — indistinguishable from "no track", so no privacy oracle.
            # Honor the dim identity set AND the live MV belt (mv_track_positions carries no dbFlags bit).
            if ctx._is_ladd_suppressed(icao, None, mv_is_ladd=False, suppress=ctx._ladd_suppress) \
                    or ctx._track_belt_suppressed(icao, time.time(), ctx._mv_ladd_hexes):
                return JSONResponse({"hex": icao, "points": []})
        # psycopg2 is sync; offload like the poller. Clicks are rare — a per-click query is cheap.
        try:
            points = await asyncio.to_thread(ctx._fetch_track, icao)
        except Exception as exc:  # RW down → empty track; selection and wake still render
            print(f"livemap track fetch failed: {exc}", flush=True)
            points = []
        # unknown hex → empty points, 200: absence is a normal state, never a 404
        return JSONResponse({"hex": icao, "points": points})

    @router.get("/flights/{hex}")
    async def flights(hex: str) -> JSONResponse:
        key = hex.lower()
        suppress = ctx._ladd_suppress

        # Public-only: the per-row callsign belt around the CH cache — private serves every row unfiltered.
        def served(rows):
            return ctx._ladd_filter_flights(rows, suppress) if ctx.PUBLIC_MODE else rows

        # Live-set LADD gate runs per-request around the CH cache (the mart's is_ladd is batch-refreshed): a newly
        # listed airframe would else keep serving history here while /aircraft, /track, /path already suppress it.
        if ctx.PUBLIC_MODE:
            # None = suppression never loaded → fail closed for every hex (mirrors /track); empty is
            # indistinguishable from no-history, so no privacy oracle.
            if suppress is None:
                return JSONResponse({"hex": hex, "flights": []})
            # Requested hex listed right now → empty before we read cache or touch CH, warm cache or not.
            if ctx._is_ladd_suppressed(key, None, mv_is_ladd=False, suppress=suppress):
                return JSONResponse({"hex": hex, "flights": []})
        now = time.time()
        hit = ctx._flights_cache.get(key)
        if hit and hit[0] > now:
            return JSONResponse({"hex": hex, "flights": served(hit[1])})
        try:
            rows = await asyncio.to_thread(ctx._fetch_flights, key)
            # cache only successes
            ctx.cache.put(ctx._flights_cache, key, (now + ctx.FLIGHTS_CACHE_TTL_S, rows), now,
                          ctx.FLIGHTS_CACHE_MAX)
        except Exception as exc:
            # type name distinguishes a real CH outage from a bug the broad never-500 catch would mask
            print(f"livemap flights fetch failed: {type(exc).__name__}: {exc}", flush=True)
            rows = []
        return JSONResponse({"hex": hex, "flights": served(rows)})

    return router
