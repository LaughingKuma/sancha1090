import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

UNAVAILABLE = {"status": "unavailable"}


def build_router(ctx) -> APIRouter:
    router = APIRouter()

    async def rooftop() -> dict:
        # Public never counts aircraft before its live LADD set has loaded, as /track fails closed.
        if ctx.PUBLIC_MODE and ctx._ladd_suppress is None:
            return dict(UNAVAILABLE)
        return await ctx._stats_cache.section(
            "rooftop", lambda: ctx.stats.compute_rooftop(ctx._ch_client))

    @router.get("/stats-data")
    async def stats_data() -> JSONResponse:
        # Always 200: each section carries its own status, and a CH outage never becomes a 500.
        roof, db = await asyncio.gather(
            rooftop(),
            ctx._stats_cache.section("database", lambda: ctx.stats.compute_database(ctx._ch_client),
                                     stale_max_s=ctx.stats.DATABASE_STALE_MAX_S),
        )
        return JSONResponse({"contract": ctx.stats.STATS_CONTRACT, "generated_at": int(time.time()),
                             "floor": ctx.stats.STATS_MIN_AIRFRAMES, "rooftop": roof, "database": db})

    return router
