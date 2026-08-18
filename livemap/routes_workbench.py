import asyncio
import time

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse


def _wb_response(payload) -> JSONResponse:
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


async def _serve_cached(store, name, key, fetcher, empty) -> JSONResponse:
    # One check/fetch/degrade/put cycle for every endpoint; `key` doubles as the fetcher's args
    # (cache identity IS the fetch identity). Never 500 — the empty envelope is honest degradation.
    cached = store.caches[name]
    now = time.time()
    hit = cached.get(key)
    if hit and hit[0] > now:
        return _wb_response(hit[1])
    try:
        payload = await asyncio.to_thread(fetcher, *key)
    except Exception as exc:
        print(f"livemap workbench {name} fetch failed: {exc.__class__.__name__}", flush=True)
        return _wb_response(empty)
    store.cache_put(cached, key, (now + store.ttls[name], payload), now, store.max_sizes[name])
    return _wb_response(payload)


def build_router(store, ctx) -> APIRouter:
    router = APIRouter()
    wb = store.wb
    # response_model documents the envelope in OpenAPI only: every handler still returns a JSONResponse,
    # which FastAPI passes through unvalidated — the never-500 degradation payloads stay untouched
    def get(path):
        return router.get(path, response_model=ctx.wb_models.ENVELOPES[path])

    @get("/features")
    async def features() -> JSONResponse:
        return _wb_response({"features": {"workbench": True}, "contract": ctx.WB_CONTRACT})

    @get("/workbench/airlines")
    async def airlines(q: str = "", limit: int = 50, offset: int = 0) -> JSONResponse:
        limit = wb.clamp(limit, 200)
        offset = max(0, offset)
        return await _serve_cached(
            store, "airlines", (q, limit, offset), store.fetch_airlines,
            {"airlines": [], "total": 0, "limit": limit, "offset": offset})

    @get("/workbench/services")
    async def services(airline: str = "", q: str = "", limit: int = 100, offset: int = 0) -> JSONResponse:
        limit = wb.clamp(limit, 500)
        offset = max(0, offset)
        return await _serve_cached(
            store, "services", (airline, q, limit, offset), store.fetch_services,
            {"services": [], "total": 0, "limit": limit, "offset": offset})

    @get("/workbench/instances")
    async def instances(callsign: str = "", airline: str = "", hex: str = "", reg: str = "",
                        airport: str = "", od: str = "", type: str = "", military: int = 0,
                        day_from: str = "", day_to: str = "", sort: str = "day_desc",
                        limit: int = 50, offset: int = 0) -> JSONResponse:
        limit = wb.clamp(limit, 500)
        offset = max(0, offset)
        df = wb.parse_day(day_from)
        dt = wb.parse_day(day_to)
        mil = bool(military)
        return await _serve_cached(
            store, "instances", (callsign, airline, hex, reg, airport, od, type, mil, df, dt, sort,
                                 limit, offset), store.fetch_instances,
            {"instances": [], "od_breakdown": [], "total": 0, "limit": limit, "offset": offset})

    @get("/workbench/summary")
    async def summary(day_from: str = "", day_to: str = "") -> JSONResponse:
        df = wb.parse_day(day_from)
        dt = wb.parse_day(day_to)
        # complete:false marks a transient fetch failure — distinct from a section's
        # available:false, which means the optional mart is not deployed
        return await _serve_cached(store, "summary", (df, dt), store.fetch_summary,
                                   wb.empty_summary() | {"complete": False})

    @get("/workbench/trends")
    async def trends(dim: str = "route", day_from: str = "", day_to: str = "",
                     _grain: str = Query("", alias="grain"),
                     limit: int = 20, offset: int = 0) -> JSONResponse:
        limit = wb.clamp(limit, 50)
        offset = max(0, offset)
        # normalize here so the cache key is canonical; grain is accepted and ignored (v1 has one grain)
        dim = dim if dim in ("route", "airline", "airport") else "route"
        df = wb.parse_day(day_from)
        dt = wb.parse_day(day_to)
        return await _serve_cached(
            store, "trends", (dim, df, dt, limit, offset), store.fetch_trends,
            wb.empty_trends(dim, limit, offset) | {"complete": False})

    @get("/workbench/flags")
    async def flags(flag_class: str = Query("", alias="class"), day_from: str = "", day_to: str = "",
                    limit: int = 50, offset: int = 0) -> JSONResponse:
        limit = wb.clamp(limit, 500)
        offset = max(0, offset)
        # canonicalize before keying (the trends-dim precedent): the fetcher strips too, so
        # whitespace-padded aliases must not mint distinct cache entries for one result
        flag_class = (flag_class or "").strip()
        df = wb.parse_day(day_from)
        dt = wb.parse_day(day_to)
        # available:false stays reserved for the missing mart; complete:false is the outage signal
        return await _serve_cached(
            store, "flags", (flag_class, df, dt, limit, offset), store.fetch_flags,
            {"available": True, "complete": False, "flags": [], "classes": {},
             "total": 0, "limit": limit, "offset": offset})

    @get("/workbench/estimates")
    async def estimates(day_from: str = "", day_to: str = "") -> JSONResponse:
        df = wb.parse_day(day_from)
        dt = wb.parse_day(day_to)
        # available:false stays reserved for a missing mart; complete:false is the outage signal
        return await _serve_cached(store, "estimates", (df, dt), store.fetch_estimates,
                                   wb.empty_estimates() | {"complete": False})

    @get("/workbench/coverage")
    async def coverage(day_from: str = "", day_to: str = "") -> JSONResponse:
        df = wb.parse_day(day_from)
        dt = wb.parse_day(day_to)
        return await _serve_cached(store, "coverage", (df, dt), store.fetch_coverage,
                                   wb.empty_coverage() | {"complete": False})

    @get("/workbench/search")
    async def search(q: str = "", limit: int = 20) -> JSONResponse:
        empty = {"airlines": [], "services": [], "airframes": [], "airports": []}
        if len((q or "").strip()) < 2:
            return _wb_response(empty)
        limit = wb.clamp(limit, 100)
        return await _serve_cached(store, "search", (q, limit), store.fetch_search, empty)

    return router
