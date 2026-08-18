import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def build_router(ctx) -> APIRouter:
    router = APIRouter()

    @router.get("/aircraft")
    async def aircraft() -> JSONResponse:
        return JSONResponse(ctx._snapshot)

    @router.get("/range-outline")
    async def range_outline() -> JSONResponse:
        # Public anchors at the coverage-outline centroid (Amit ruling 2026-07-24): a pure function
        # of the already-served ring leaks nothing the polygon doesn't; the real receiver stays private.
        if ctx.PUBLIC_MODE:
            return JSONResponse({"center": ctx._ring_centroid(ctx._outline), "center_kind": "coverage",
                                 "ring": ctx._outline})
        return JSONResponse({"center": [ctx.FEEDER_LON, ctx.FEEDER_LAT], "center_kind": "receiver",
                             "ring": ctx._outline})

    @router.get("/history")
    async def history(s: float = 90.0) -> JSONResponse:
        s = min(s if s > 0 else 90.0, ctx.HISTORY_BUFFER_S)
        cutoff = time.time() - s
        snaps = [[ts, rows] for ts, rows in ctx._track_buf if ts >= cutoff]
        return JSONResponse({"snapshots": snaps})

    @router.get("/healthz")
    async def healthz() -> JSONResponse:
        fresh = (time.time() - ctx._snapshot["server_ts"]) < 10
        payload = {
            "ok": fresh,
            "count": len(ctx._snapshot["aircraft"]),
            "server_ts": ctx._snapshot["server_ts"],
            "static_build": ctx._static_build(),
        }
        # counters tick synchronously per enqueue and pre-gate denials never enqueue — public
        # exposure would let callers bracket a probe and pierce the uniform denial wire (rev 10.2)
        if not ctx.PUBLIC_MODE:
            payload["est_log"] = {
                "queued": ctx._est_log_queue.groups,
                "dropped": ctx._est_log_queue.dropped,
                "accepted": ctx._est_log_queue.accepted,
                "written": ctx._est_log_queue.written,
            }
        return JSONResponse(payload, status_code=200 if fresh else 503)

    return router
