import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def _wb_response(payload) -> JSONResponse:
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


def build_router(ctx) -> APIRouter:
    router = APIRouter()

    @router.get("/features")
    async def features() -> JSONResponse:
        return _wb_response({"features": {"workbench": True}})

    @router.get("/workbench/airlines")
    async def airlines(q: str = "", limit: int = 50, offset: int = 0) -> JSONResponse:
        limit = ctx.wb.clamp(limit, 200)
        offset = max(0, offset)
        key = (q, limit, offset)
        now = time.time()
        hit = ctx._wb_airlines_cache.get(key)
        if hit and hit[0] > now:
            return _wb_response(hit[1])
        try:
            payload = await asyncio.to_thread(ctx._fetch_wb_airlines, q, limit, offset)
        except Exception as exc:  # never 500 — the empty envelope is a servable, honest degradation
            print(f"livemap workbench airlines fetch failed: {type(exc).__name__}", flush=True)
            return _wb_response({"airlines": [], "total": 0, "limit": limit, "offset": offset})
        ctx.cache.put(ctx._wb_airlines_cache, key, (now + ctx.WB_AIRLINES_CACHE_TTL_S, payload),
                      now, ctx.WB_AIRLINES_CACHE_MAX)
        return _wb_response(payload)

    @router.get("/workbench/services")
    async def services(airline: str = "", q: str = "", limit: int = 100, offset: int = 0) -> JSONResponse:
        limit = ctx.wb.clamp(limit, 500)
        offset = max(0, offset)
        key = (airline, q, limit, offset)
        now = time.time()
        hit = ctx._wb_services_cache.get(key)
        if hit and hit[0] > now:
            return _wb_response(hit[1])
        try:
            payload = await asyncio.to_thread(ctx._fetch_wb_services, airline, q, limit, offset)
        except Exception as exc:
            print(f"livemap workbench services fetch failed: {type(exc).__name__}", flush=True)
            return _wb_response({"services": [], "total": 0, "limit": limit, "offset": offset})
        ctx.cache.put(ctx._wb_services_cache, key, (now + ctx.WB_SERVICES_CACHE_TTL_S, payload),
                      now, ctx.WB_SERVICES_CACHE_MAX)
        return _wb_response(payload)

    @router.get("/workbench/instances")
    async def instances(callsign: str = "", airline: str = "", hex: str = "", reg: str = "",
                        airport: str = "", od: str = "", type: str = "", military: int = 0,
                        day_from: str = "", day_to: str = "", sort: str = "day_desc",
                        limit: int = 50, offset: int = 0) -> JSONResponse:
        limit = ctx.wb.clamp(limit, 500)
        offset = max(0, offset)
        df = ctx.wb.parse_day(day_from)
        dt = ctx.wb.parse_day(day_to)
        mil = bool(military)
        key = (callsign, airline, hex, reg, airport, od, type, mil, df, dt, sort, limit, offset)
        now = time.time()
        hit = ctx._wb_instances_cache.get(key)
        if hit and hit[0] > now:
            return _wb_response(hit[1])
        try:
            payload = await asyncio.to_thread(
                ctx._fetch_wb_instances, callsign, airline, hex, reg, airport, od, type,
                mil, df, dt, sort, limit, offset,
            )
        except Exception as exc:
            # NOT type(exc) — the `type` query param shadows the builtin in this handler's scope
            print(f"livemap workbench instances fetch failed: {exc.__class__.__name__}", flush=True)
            return _wb_response({"instances": [], "od_breakdown": [], "total": 0,
                                 "limit": limit, "offset": offset})
        ctx.cache.put(ctx._wb_instances_cache, key, (now + ctx.WB_INSTANCES_CACHE_TTL_S, payload),
                      now, ctx.WB_INSTANCES_CACHE_MAX)
        return _wb_response(payload)

    @router.get("/workbench/search")
    async def search(q: str = "", limit: int = 20) -> JSONResponse:
        empty = {"airlines": [], "services": [], "airframes": [], "airports": []}
        if len((q or "").strip()) < 2:
            return _wb_response(empty)
        limit = ctx.wb.clamp(limit, 100)
        key = (q, limit)
        now = time.time()
        hit = ctx._wb_search_cache.get(key)
        if hit and hit[0] > now:
            return _wb_response(hit[1])
        try:
            payload = await asyncio.to_thread(ctx._fetch_wb_search, q, limit)
        except Exception as exc:
            print(f"livemap workbench search fetch failed: {type(exc).__name__}", flush=True)
            return _wb_response(empty)
        ctx.cache.put(ctx._wb_search_cache, key, (now + ctx.WB_SEARCH_CACHE_TTL_S, payload),
                      now, ctx.WB_SEARCH_CACHE_MAX)
        return _wb_response(payload)

    return router
