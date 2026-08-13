import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def _path_response(flight_id: str, points, provisional: bool) -> JSONResponse:
    # no-store on EVERY branch: a future CF rule or edge default must never cache geometry past a LADD flip
    return JSONResponse(
        {"flight_id": flight_id, "points": points, "provisional": provisional},
        headers={"Cache-Control": "no-store"},
    )


def _estimate_response(payload, estimate_id=None) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if estimate_id is not None:
        # server-generated causal key for deploy verification (gate C queries these exact
        # UUIDs); only on never-cached provisional/live successes — never settled/empty
        headers["X-Estimate-Id"] = str(estimate_id)
    return JSONResponse(payload, headers=headers)


def _empty_estimate(ctx, flight_id, reason, provisional, as_of) -> dict:
    return ctx.ess.build_response(
        flight_id,
        ctx.est.EstimateResult([], [{"kind": "all", "reason": reason}], []),
        provisional,
        as_of,
    )


def _empty_live_estimate(ctx, icao24: str) -> dict:
    return ctx.ess.build_live_response(
        icao24,
        ctx.est.EstimateResult([], [{"kind": "all", "reason": "no_input"}], []),
        0,
    )


def _live_anchor(row):
    # alt_baro is the MV's string field: numeric text or the 'ground' sentinel (design §5)
    raw = row.get("alt_baro")
    on_ground = isinstance(raw, str) and raw.strip().lower() == "ground"
    alt_ft = None
    if not on_ground and raw is not None:
        try:
            alt_ft = float(raw)
        except (TypeError, ValueError):
            alt_ft = None
    return (row["capture_ts"], row["lat"], row["lon"], alt_ft, int(on_ground),
            row.get("gs"), row.get("track"), "live")


async def _serve_estimate(ctx, flight_id: str, fid: int, got, *, settled: bool) -> JSONResponse:
    try:
        od, flight = await asyncio.to_thread(ctx._fetch_od, fid)
    except Exception as exc:
        print(f"livemap estimate O/D fetch failed: {type(exc).__name__}: {exc}", flush=True)
        return _estimate_response(_empty_estimate(ctx, flight_id, "no_input", False, 0))

    route_pts, plan_ts = await ctx._route_prior(got["points"], flight)
    fp = await asyncio.to_thread(ctx.ess.input_fingerprint, got["points"], od, route_pts, plan_ts)
    now = time.time()
    # cache policy is arm-explicit: settled looks up and puts, provisional touches neither. PR-3:
    # provisional inputs are estimated and served, logged input_provisional=1, and NEVER cached —
    # same invariant (and reason) as the rung-2 _path_cache bypass
    key = (fid, fp, ctx.ess.METHOD_VERSION) if settled else None

    if settled:
        hit = ctx._est_cache.get(key)
        if hit and hit[0] > now:
            # public-only re-check: private suppresses nothing, so a cache hit is always servable there
            if ctx.PUBLIC_MODE:
                icao24, callsign, mart_ladd = got["auth"][:3]
                if ctx._is_ladd_suppressed(
                    icao24,
                    callsign,
                    mv_is_ladd=mart_ladd,
                    suppress=ctx._ladd_suppress,
                ):
                    return _estimate_response(_empty_estimate(ctx, flight_id, "no_input", False, 0))
            # the canonical fid key makes 42/042 share an entry — echo the CALLER's spelling, not the seeder's
            return _estimate_response({**hit[1], "flight_id": flight_id})

    r = await asyncio.to_thread(ctx.est.estimate, got["points"], od, route_pts=route_pts)
    ctx._stamp_route_plan(r, plan_ts)
    payload = ctx.ess.build_response(flight_id, r, not settled, int(got["as_of"]))
    if settled:
        ctx._est_cache_put(key, payload, now)
    eid = ctx.ess.new_estimate_id()
    ctx._enqueue_estimate_log(
        ctx.ess.build_log_rows(
            eid,
            fid,
            got["auth"][0],
            r,
            payload,
            got["points"],
            fp,
            ctx.ess.utcnow(),
            producer=ctx.EST_PRODUCER,
        )
    )
    if settled:
        return _estimate_response(payload)
    # the causal key rides only segments-bearing provisional responses — empties stay header-uniform
    return _estimate_response(payload, eid if payload["segments"] else None)


def build_router(ctx) -> APIRouter:
    router = APIRouter()

    @router.get("/path/{flight_id}")
    async def path(flight_id: str) -> JSONResponse:
        got = await ctx._load_path_input(flight_id)
        # provisional rides EVERY provisional-arm response, empty included — the client owns the n>0 logic
        return _path_response(flight_id, ctx._lean_points(got["points"]), got["status"] == "provisional")

    @router.get("/path/{flight_id}/estimate")
    async def path_estimate(flight_id: str) -> JSONResponse:
        got = await ctx._load_path_input(flight_id)
        if got["status"] == "denied":
            return _estimate_response(_empty_estimate(ctx, flight_id, "no_input", False, 0))

        fid = int(flight_id)
        if got["status"] == "provisional":
            return await _serve_estimate(ctx, flight_id, fid, got, settled=False)

        if got["status"] == "settled_empty":
            r = ctx.est.estimate([], ctx.est.OD())
            payload = ctx.ess.build_response(flight_id, r, False, int(got["as_of"]))
            fp = ctx.ess.input_fingerprint([], ctx.est.OD(), None)
            ctx._enqueue_estimate_log(
                ctx.ess.build_log_rows(
                    ctx.ess.new_estimate_id(),
                    fid,
                    got["auth"][0],
                    r,
                    payload,
                    [],
                    fp,
                    ctx.ess.utcnow(),
                    producer=ctx.EST_PRODUCER,
                )
            )
            return _estimate_response(payload)

        return await _serve_estimate(ctx, flight_id, fid, got, settled=True)

    @router.get("/estimate/live/{icao24}")
    async def estimate_live(icao24: str) -> JSONResponse:
        # Every denial — unknown, stale, aged, LADD, suppress-None, and computed non-results —
        # serves this ONE shape; the truthful record of post-gate computations lives in the log only.
        empty = _empty_live_estimate(ctx, icao24)
        key = icao24.strip().lower()
        if not ctx._LIVE_HEX_RE.fullmatch(key):
            return _estimate_response(empty)   # invalid input is a PRE-GATE denial (rev 9)
        suppress = ctx._ladd_suppress
        # public-only fail-closed: private never loads a set, so the None check would deny every request
        if ctx.PUBLIC_MODE and suppress is None:
            return _estimate_response(empty)
        now = time.time()
        snap = ctx._snapshot
        if not (-ctx.EST_LIVE_FUTURE_SKEW_S <= now - snap["server_ts"] <= ctx.EST_LIVE_SNAP_FRESH_S):
            return _estimate_response(empty)
        row = next((a for a in snap["aircraft"] if (a.get("hex") or "").strip().lower() == key), None)
        if row is None:
            return _estimate_response(empty)
        if ctx.PUBLIC_MODE and (
            ctx._is_ladd_suppressed(key, row.get("flight"), mv_is_ladd=False, suppress=suppress)
            or ctx._track_belt_suppressed(key, now, ctx._mv_ladd_hexes)
        ):
            return _estimate_response(empty)
        ct = row.get("capture_ts")
        # the chained comparison also rejects NaN ages (any comparison with NaN is False)
        if ct is None or not (-ctx.EST_LIVE_FUTURE_SKEW_S <= now - ct <= ctx.EST_LIVE_MAX_AGE_S):
            return _estimate_response(empty)
        anchor = _live_anchor(row)
        r = await asyncio.to_thread(ctx.est.estimate, [anchor], ctx.est.OD())
        payload = ctx.ess.build_live_response(icao24, r, int(snap["server_ts"]))
        # live DR carries no route prior (gap bridges only) — the fingerprint records its absence
        fp = await asyncio.to_thread(ctx.ess.input_fingerprint, [anchor], ctx.est.OD(), None)
        eid = ctx.ess.new_estimate_id()
        ctx._enqueue_estimate_log(
            ctx.ess.build_log_rows(
                eid,
                None,
                key,
                r,
                payload,
                [anchor],
                fp,
                ctx.ess.utcnow(),
                anchor_ts=anchor[0],
                producer=ctx.EST_PRODUCER,
            )
        )
        if not payload["segments"]:
            return _estimate_response(empty)
        return _estimate_response(payload, eid)

    return router
