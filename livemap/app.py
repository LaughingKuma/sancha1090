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

# LADD suppression: the live surfaces only need "listed right now" — the OPEN intervals. The mart's
# window-aware is_ladd covers history. dim_ladd is RMT(_version) so FINAL for current SCD2 state.
LADD_SUPPRESS_QUERY = "SELECT icao24, callsign FROM dim.dim_ladd FINAL WHERE valid_to IS NULL"
LADD_REFRESH_SECONDS = float(os.environ.get("LIVEMAP_LADD_REFRESH_SECONDS", "900"))
LADD_REFRESH_TICKS = max(1, int(LADD_REFRESH_SECONDS / POLL_SECONDS))
# Last-good suppression sets on disk: a restart mid-CH-cold-start reseeds from this instead of failing open
# (None). The container FS survives restarts, so the fail-open window collapses to first-ever boot/recreate.
LADD_CACHE_PATH = os.environ.get("LIVEMAP_LADD_CACHE_PATH", "/tmp/ladd_suppress_cache.json")

# Real antenna is a secret (home rooftop) — from .env; default is the public Carrot Tower landmark.
FEEDER_LAT = float(os.environ.get("LIVEMAP_FEEDER_LAT", "35.6434"))
FEEDER_LON = float(os.environ.get("LIVEMAP_FEEDER_LON", "139.6692"))

# Public-instance hardening — every effect below is gated on this flag so the private LAN instance is
# byte-identical when it is unset (the middleware isn't even registered).
PUBLIC_MODE = os.environ.get("LIVEMAP_PUBLIC_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
# Per-IP token bucket on the per-request DB endpoints only (/aircraft + /history are in-memory reads, free).
RATE_LIMITED_PREFIXES = ("/track/", "/flights/", "/path/")
RATE_LIMIT_BURST = float(os.environ.get("LIVEMAP_RATE_LIMIT_BURST", "10"))
RATE_LIMIT_REFILL_PER_SEC = float(os.environ.get("LIVEMAP_RATE_LIMIT_REFILL_PER_SEC", "1"))
# Cap on tracked IP buckets; fully-refilled idle ones are swept lazily on access past this cap.
RATE_BUCKETS_MAX = int(os.environ.get("LIVEMAP_RATE_BUCKETS_MAX", "65536"))

# recv rides the payload end-to-end (the P2 multi-receiver seam); rendered uniformly today.
QUERY = """
    SELECT capture_ts, hex, flight, lat, lon, alt_baro, gs, track,
           typecode, aircraft_desc, registration, body_class, is_military, is_helicopter, is_ladd,
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

# "Where else has it been": one clean source now — the reconciled consensus mart (SP2) carries
# resolved O/D + endpoint geo + provenance, so no read-time fact_flights/legs UNION or watermark.
# flight_id is cityHash64 → UInt64; toString keeps it exact (JS Number can't hold it) and keys /path.
FLIGHTS_QUERY = f"""
    SELECT 'reconciled' AS src, end_time AS ts,
           coalesce(origin_iata, origin_icao) AS o_code, coalesce(origin_city, origin_name) AS o_name,
           coalesce(dest_iata,   dest_icao)   AS d_code, coalesce(dest_city,   dest_name)   AS d_name, callsign,
           toString(flight_id) AS flight_id
    FROM {CH_DB}.fct_flights_reconciled
    WHERE icao24 = {{hex:String}} AND is_ladd = 0 AND (origin_icao IS NOT NULL OR dest_icao IS NOT NULL)
    ORDER BY ts DESC LIMIT 10
"""

# Historical fused trajectory for one reconciled flight; LADD suppression rides the is_ladd=0 subquery
# (window-aware) so a listed flight yields zero rows — indistinguishable from no-path, no privacy oracle.
PATH_QUERY = f"""
    SELECT toUnixTimestamp(ts), lon, lat, alt_ft, source
    FROM {CH_DB}.fct_flight_path
    WHERE flight_id = {{fid:UInt64}}
      AND flight_id IN (
        SELECT flight_id FROM {CH_DB}.fct_flights_reconciled
        WHERE flight_id = {{fid:UInt64}} AND is_ladd = 0
      )
    ORDER BY ts
"""

# Per-click authorization (also on geometry-cache hits): closes stale-LADD/stale-id windows without giving
# up the geometry cache; the window + start day ride along for the provisional arm (one execution).
PATH_AUTH_QUERY = f"""
    SELECT lower(icao24), callsign, is_ladd,
           toUnixTimestamp(start_time), toUnixTimestamp(end_time), toDate(start_time)
    FROM {CH_DB}.fct_flights_reconciled
    WHERE flight_id = {{fid:UInt64}}
    LIMIT 1
"""

# Overlapping same-hex windows contest fixes in-process (mart stage-1 rule); deliberately is_ladd-blind — a
# suppressed neighbor still wins its own fixes away. ov_lo/ov_hi pre-bake ±pad on both windows (2·pad).
PATH_COMPETITOR_QUERY = f"""
    SELECT flight_id, toUnixTimestamp(start_time), toUnixTimestamp(end_time)
    FROM {CH_DB}.fct_flights_reconciled
    WHERE lower(icao24) = {{hex:String}} AND flight_id != {{fid:UInt64}}
      AND start_time <= toDateTime({{ov_hi:Int64}}) AND end_time >= toDateTime({{ov_lo:Int64}})
"""

# The three bronze scans mirror fct_flight_path's fixes CTEs (timestamp choice, units, sentinels) with
# physical leading-key predicates; uniform projection (ts_s, lat, lon, alt_ft, on_ground, gs_kt, track_deg).
PROVISIONAL_ADSB_QUERY = """
    SELECT toUInt32(floor(assumeNotNull(capture_ts))), lat, lon,
           if(alt_baro = 'ground', 0, toFloat64OrNull(alt_baro)),
           toUInt8(coalesce(alt_baro, '') = 'ground'), gs, track
    FROM bronze.adsb_states
    WHERE capture_date BETWEEN {day_lo:Date} AND {day_hi:Date}
      AND lower(hex) = {hex:String}
      AND capture_ts IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL
      AND capture_ts >= {win_lo:Int64} AND capture_ts < {win_hi:Int64} + 1
"""

PROVISIONAL_ADSBLOL_QUERY = """
    SELECT toUnixTimestamp(toDateTime(assumeNotNull(ts), 'UTC')), lat, lon, alt_ft,
           toUInt8(coalesce(on_ground, false)), gs_kt, track_deg
    FROM bronze.adsblol_flight_paths
    WHERE trace_day BETWEEN {halo_lo:Date} AND {halo_hi:Date}
      AND lower(icao24) = {hex:String}
      AND ts IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL
      AND ts >= toDateTime64({win_lo:Int64}, 6, 'UTC') AND ts < toDateTime64({win_hi:Int64} + 1, 6, 'UTC')
"""

# snapshot_date halo ±1 AND the broad snapshot_time range (the primary key LEADS with snapshot_time; the
# 10,300 s skew tail rules out fixed slack). The event-time clip does the precision work.
PROVISIONAL_OPENSKY_QUERY = """
    SELECT toUnixTimestamp(toDateTime(assumeNotNull(coalesce(time_position, snapshot_time)), 'UTC')),
           latitude, longitude, baro_altitude * 3.28084,
           toUInt8(coalesce(on_ground, false)), velocity * 1.94384, true_track
    FROM bronze.opensky_states
    WHERE snapshot_date BETWEEN {halo_lo:Date} AND {halo_hi:Date}
      AND snapshot_time >= toDateTime64({broad_lo:Int64}, 6, 'UTC')
      AND snapshot_time < toDateTime64({broad_hi:Int64}, 6, 'UTC')
      AND lower(icao24) = {hex:String}
      AND coalesce(time_position, snapshot_time) IS NOT NULL
      AND latitude IS NOT NULL AND longitude IS NOT NULL
      AND toDateTime(assumeNotNull(coalesce(time_position, snapshot_time)), 'UTC')
          BETWEEN toDateTime({win_lo:Int64}, 'UTC') AND toDateTime({win_hi:Int64}, 'UTC')
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
# LADD open-interval identities (hex + normalized callsign) refreshed from CH every ~15 min; suppressed on the
# live surfaces. _EMPTY_SUPPRESS is the sentinel for a real, loaded, currently-empty list.
_EMPTY_SUPPRESS: dict = {"hex": frozenset(), "callsign": frozenset()}


def _write_ladd_cache(suppress, path) -> None:
    # Atomic last-good write (temp + os.replace) so a crash mid-write never leaves a half-JSON the boot seed trusts.
    tmp = f"{path}.tmp"
    with open(tmp, "w") as fh:
        json.dump({"hex": sorted(suppress["hex"]), "callsign": sorted(suppress["callsign"])}, fh)
    os.replace(tmp, path)


def _read_ladd_cache(path):
    # Boot seed from the last-good sets so a CH-cold-start restart resumes dim filtering instead of failing open.
    # Conservative-stale is the right direction (the list mostly grows); missing/corrupt -> None, a never-loaded
    # cold start exactly as before the cache existed.
    try:
        with open(path) as fh:
            d = json.load(fh)
        return {"hex": frozenset(d["hex"]), "callsign": frozenset(d["callsign"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cache_ladd_suppress(suppress) -> None:
    # Best-effort: a read-only/full FS must never break the live refresh — the in-memory set stays authoritative.
    try:
        _write_ladd_cache(suppress, LADD_CACHE_PATH)
    except Exception as exc:
        print(f"livemap ladd suppress cache write skipped: {exc}", flush=True)


# None = never loaded (fails /track closed, logs once); empty frozensets = a real, loaded, currently-empty list.
# Boot-seeded from the last-good disk cache so a restart during a CH cold start resumes dim filtering, not None.
_ladd_suppress: dict | None = _read_ladd_cache(LADD_CACHE_PATH)
_ladd_none_warned: bool = False
# Hexes _fetch dropped for the MV's is_ladd bit, keyed to the time last seen. mv_track_positions carries no
# dbFlags, so /track fails closed on a hex still in this TTL'd set (retained for HISTORY_BUFFER_S).
_mv_ladd_hexes: dict = {}
# /flights is on-click + rarely changing (the reconciled mart is batch) — cache per hex.
_flights_cache: dict = {}
FLIGHTS_CACHE_TTL_S = float(os.environ.get("LIVEMAP_FLIGHTS_CACHE_TTL_S", "120"))
try:
    # bad env falls back instead of crashing import; floor 0 so the eviction loop always terminates
    FLIGHTS_CACHE_MAX = max(0, int(os.environ.get("LIVEMAP_FLIGHTS_CACHE_MAX", "512")))
except ValueError:
    FLIGHTS_CACHE_MAX = 512

# /path geometry is expensive but authorization is cheap; only geometry rides this longer cache.
_path_cache: dict = {}
PATH_CACHE_TTL_S = float(os.environ.get("LIVEMAP_PATH_CACHE_TTL_S", "900"))
try:
    PATH_CACHE_MAX = max(0, int(os.environ.get("LIVEMAP_PATH_CACHE_MAX", "256")))
except ValueError:
    PATH_CACHE_MAX = 256
UINT64_MAX = 2**64 - 1

# Provisional fallback (rung 2): flights newer than the mart's built head get a serve-time fused path.
# 600 s mirrors dbt path_window_pad_min: 10 — mart windows are observation clips, 5.97% of path rows are pad-only.
PATH_WINDOW_PAD_S = 600
PATH_HEAD_QUERY = f"SELECT max(day_key) FROM {CH_DB}.fct_flight_path"
PATH_HEAD_TTL_S = float(os.environ.get("LIVEMAP_PATH_HEAD_TTL_S", "60"))
# None = never loaded (callers fail closed); a failed refresh keeps last-good — the head only ever advances,
# so staleness is bounded and harmless in the historical direction.
_path_head: dict = {"expiry": 0.0, "head": None}


def _path_response(flight_id: str, points, provisional: bool) -> JSONResponse:
    # no-store on EVERY branch: a future CF rule or edge default must never cache geometry past a LADD flip
    return JSONResponse(
        {"flight_id": flight_id, "points": points, "provisional": provisional},
        headers={"Cache-Control": "no-store"},
    )


def _path_cache_put(fid: int, points, now: float) -> None:
    _path_cache[fid] = (now + PATH_CACHE_TTL_S, points)
    if len(_path_cache) > PATH_CACHE_MAX:
        # entries only age out on same-key hits, so a many-flight sweep would grow the dict unboundedly
        for k in [k for k, v in _path_cache.items() if v[0] <= now]:
            del _path_cache[k]
        while len(_path_cache) > PATH_CACHE_MAX:
            del _path_cache[min(_path_cache, key=lambda k: _path_cache[k][0])]


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
    global _ladd_none_warned
    now = time.time()
    rows = _rw_rows(QUERY, cursor_factory=psycopg2.extras.RealDictCursor)
    suppress = _ladd_suppress
    if suppress is None and not _ladd_none_warned:
        # Visible-once window: /aircraft leans on the MV is_ladd belt below; /track fails closed until loaded.
        print("livemap ladd suppress: not loaded yet -> /aircraft on MV belt, /track fail-closed", flush=True)
        _ladd_none_warned = True
    aircraft = []
    for r in rows:
        a = dict(r)
        ct = a["capture_ts"]
        a["capture_ts"] = ct.timestamp() if ct is not None else None
        flight = (a["flight"] or "").strip() or None
        a["flight"] = flight
        # LADD: drop currently-listed airframes before they reach any client (belt to the mart's flag).
        # pop so the flag never rides the payload; .get/.pop tolerate a partial row (test doubles).
        mv_is_ladd = a.pop("is_ladd", None)
        hex_ = a.get("hex")
        if mv_is_ladd:
            # record the belt-suppressed hex so /track (dbFlags-blind) can also fail closed for it
            h = (hex_ or "").strip().lower()
            if h:
                _mv_ladd_hexes[h] = now
        if _is_ladd_suppressed(hex_, flight, mv_is_ladd=mv_is_ladd, suppress=suppress):
            continue
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
    # drop belt entries not re-seen within the TTL so /track stops suppressing a hex that has gone quiet
    for h in [h for h, ts in _mv_ladd_hexes.items() if now - ts > HISTORY_BUFFER_S]:
        del _mv_ladd_hexes[h]
    return {"server_ts": now, "aircraft": aircraft}


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
    for src, ts, o_code, o_name, d_code, d_name, callsign, flight_id in res.result_rows:
        out.append({
            "src": src,
            # CH driver returns naive UTC datetimes — pin tzinfo so process TZ can't skew epochs
            "ts": ts.replace(tzinfo=datetime.timezone.utc).timestamp() if ts is not None else None,
            "origin": {"code": o_code, "name": o_name},
            "dest": {"code": d_code, "name": d_name},
            "callsign": (callsign or "").strip() or None,
            # decimal string, not a number: cityHash64 UInt64 overflows JS Number, so it must stay text end-to-end
            "flight_id": flight_id,
        })
    return out


def _ladd_filter_flights(rows, suppress) -> list:
    # Per-row callsign belt at serve time (around the CH cache): the hex is already cleared upstream, so drop only
    # rows on a currently-listed callsign — a listing added after the flight escapes the window-scoped mart is_ladd.
    return [r for r in rows
            if not _is_ladd_suppressed(None, r.get("callsign"), mv_is_ladd=False, suppress=suppress)]


def _valid_flight_id(fid: str) -> bool:
    # len cap MUST be first — it keeps int() under CPython's 4300-digit str->int limit (which raises ValueError,
    # and this runs outside the endpoint's try/except). UInt64 max is 20 digits.
    return len(fid) <= 20 and fid.isascii() and fid.isdigit() and int(fid) <= UINT64_MAX


def _fetch_path(flight_id: str) -> list:
    client = _ch_client()
    try:
        res = client.query(PATH_QUERY, parameters={"fid": int(flight_id)})
    finally:
        client.close()
    # lean array-of-arrays mirroring /track: [lon, lat, ts_epoch, alt_ft, source]
    return [[lon, lat, ts, alt, source] for ts, lon, lat, alt, source in res.result_rows]


def _fetch_path_auth(flight_id: str):
    client = _ch_client()
    try:
        res = client.query(PATH_AUTH_QUERY, parameters={"fid": int(flight_id)})
    finally:
        client.close()
    if not res.result_rows:
        return None
    icao24, callsign, is_ladd, start_s, end_s, start_day = res.result_rows[0]
    return icao24, callsign, bool(is_ladd), start_s, end_s, start_day


def _fetch_path_head():
    client = _ch_client()
    try:
        res = client.query(PATH_HEAD_QUERY)
    finally:
        client.close()
    head = res.result_rows[0][0] if res.result_rows else None
    # empty mart (max → NULL): everything is post-head — the fallback serves until the first build lands
    return head or datetime.date(1970, 1, 1)


def _get_path_head(now):
    if _path_head["head"] is not None and _path_head["expiry"] > now:
        return _path_head["head"]
    try:
        head = _fetch_path_head()
    except Exception as exc:
        print(f"livemap path head fetch failed: {type(exc).__name__}: {exc}", flush=True)
        return _path_head["head"]
    _path_head["expiry"] = now + PATH_HEAD_TTL_S
    _path_head["head"] = head
    return head


def _contest_keep(ts, fid, start_s, end_s, competitors) -> bool:
    # Mart stage-1 in-process: each fix goes to the nearest unpadded-window midpoint among PADDED windows
    # containing it, flight_id tiebreak — a total order, so no ordering ambiguity can flip a winner.
    best = (abs(ts - (start_s + end_s) // 2), fid)
    for cfid, cs, ce in competitors:
        if cs - PATH_WINDOW_PAD_S <= ts <= ce + PATH_WINDOW_PAD_S:
            cand = (abs(ts - (cs + ce) // 2), cfid)
            if cand < best:
                best = cand
    return best[1] == fid


def _fuse_points(rows) -> list:
    # Mart stage-2: one row per whole second by src_rank then full-tuple total order, NULLS LAST like CH —
    # a naive tuple sort mixing None and float raises, so nullable fields sort as (is None, value).
    def key(r):
        ts, rank, lat, lon, alt, gs, trk, og, _src = r
        return (ts, rank, lat, lon, (alt is None, alt), (gs is None, gs), (trk is None, trk), og)

    out, last_ts = [], None
    for ts, _rank, lat, lon, alt, _gs, _trk, _og, src in sorted(rows, key=key):
        if ts == last_ts:
            continue
        last_ts = ts
        out.append([lon, lat, ts, alt, src])
    return out


def _fetch_provisional(fid, icao24, start_s, end_s):
    # Raises on ANY stage failure — the endpoint catches to empty. A partial fusion must never masquerade as
    # a complete path, and a failed competitor lookup must never read as "zero competitors".
    win_lo, win_hi = start_s - PATH_WINDOW_PAD_S, end_s + PATH_WINDOW_PAD_S
    utc = datetime.timezone.utc
    day_lo = datetime.datetime.fromtimestamp(win_lo, tz=utc).date()
    day_hi = datetime.datetime.fromtimestamp(win_hi, tz=utc).date()
    one = datetime.timedelta(days=1)

    def day_epoch(d):
        return int(datetime.datetime.combine(d, datetime.time(), tzinfo=utc).timestamp())

    client = _ch_client()
    try:
        comp = client.query(PATH_COMPETITOR_QUERY, parameters={
            "hex": icao24, "fid": fid,
            "ov_lo": start_s - 2 * PATH_WINDOW_PAD_S, "ov_hi": end_s + 2 * PATH_WINDOW_PAD_S,
        }).result_rows
        competitors = [(int(c), int(s), int(e)) for c, s, e in comp]
        window = {"hex": icao24, "win_lo": win_lo, "win_hi": win_hi}
        rows = []
        for src, rank, query, params in (
            ("adsb", 1, PROVISIONAL_ADSB_QUERY, {"day_lo": day_lo, "day_hi": day_hi}),
            ("adsblol", 2, PROVISIONAL_ADSBLOL_QUERY, {"halo_lo": day_lo - one, "halo_hi": day_hi + one}),
            ("opensky", 3, PROVISIONAL_OPENSKY_QUERY,
             {"halo_lo": day_lo - one, "halo_hi": day_hi + one,
              "broad_lo": day_epoch(day_lo - one), "broad_hi": day_epoch(day_hi + one) + 86400}),
        ):
            for ts, lat, lon, alt, og, gs, trk in client.query(query, parameters={**window, **params}).result_rows:
                # defensive re-clip + null-geometry drop: the fusion contract owns them, not just the scans
                if ts is None or lat is None or lon is None:
                    continue
                ts = int(ts)
                if ts < win_lo or ts > win_hi:
                    continue
                rows.append((ts, rank, lat, lon, alt, gs, trk, int(og or 0), src))
    finally:
        client.close()
    return _fuse_points([r for r in rows if _contest_keep(r[0], fid, start_s, end_s, competitors)])


def _is_ladd_suppressed(hex_, callsign, mv_is_ladd, suppress) -> bool:
    # Pure: True if the row is LADD-listed by the MV's db_flags bit OR by an open-interval hex/callsign identity.
    # suppress None = the dim set never loaded; only the MV belt applies here (callers fail /track closed).
    if mv_is_ladd:
        return True
    if suppress is None:
        return False
    h = (hex_ or "").strip().lower()
    if h and h in suppress["hex"]:
        return True
    c = (callsign or "").strip().upper()
    return bool(c and c in suppress["callsign"])


def _should_refresh_ladd(state, tick) -> bool:
    # None = never loaded: retry every poll tick until the first success closes the fail-open window (a host
    # reboot boots livemap before CH is healthy). Once loaded (even empty) revert to the ~15-min cadence.
    return state is None or tick % LADD_REFRESH_TICKS == 0


def _track_belt_suppressed(hex_, now, mv_ladd_hexes) -> bool:
    # Pure: /track can't see the MV is_ladd bit (mv_track_positions carries no dbFlags), so honor the live belt
    # _fetch maintains — a hex dropped for mv_is_ladd within the last HISTORY_BUFFER_S.
    ts = mv_ladd_hexes.get((hex_ or "").strip().lower())
    return ts is not None and (now - ts) <= HISTORY_BUFFER_S


def _is_unknown_table_error(exc) -> bool:
    # clickhouse-connect sets code/name on DatabaseError; code 60 / UNKNOWN_TABLE = a missing relation. Prefer the
    # structured code, then the symbolic name, then fall back to the server text (Code: 60 / UNKNOWN_TABLE, and
    # the pre-structured "doesn't exist" wording so an older/plain error still resolves).
    code = getattr(exc, "code", None)
    if code is not None and str(code) == "60":
        return True
    if str(getattr(exc, "name", "") or "").upper() == "UNKNOWN_TABLE":
        return True
    s = str(exc).lower()
    return "code: 60" in s or "unknown_table" in s or "unknown table" in s or "doesn't exist" in s


def _ladd_missing_table(exc) -> bool:
    # Pre-deploy, dim.dim_ladd doesn't exist yet — expected cold-start, not an outage. Scope the UNKNOWN_TABLE
    # signal to dim_ladd so any *other* missing relation still surfaces as a real error.
    return "dim_ladd" in str(exc).lower() and _is_unknown_table_error(exc)


def _fetch_ladd_suppress() -> dict:
    client = _ch_client()
    try:
        res = client.query(LADD_SUPPRESS_QUERY)
    finally:
        client.close()
    hexes = {i.strip().lower() for i, _ in res.result_rows if i}
    calls = {c.strip().upper() for _, c in res.result_rows if c}
    return {"hex": frozenset(hexes), "callsign": frozenset(calls)}


def _refresh_ladd_suppress(current):
    # Graduated fail-closed: a real refresh error keeps the current state (None stays None -> the surfaces fail
    # closed; a loaded set stays as-is). A MISSING dim_ladd is the pre-deploy state — a *successful* empty load,
    # so None -> empty and live filtering resumes normal (belt-only) behavior.
    try:
        fresh = _fetch_ladd_suppress()
    except Exception as exc:
        if _ladd_missing_table(exc):
            print(f"livemap ladd suppress: dim_ladd absent (pre-deploy) -> empty load: {exc}", flush=True)
            return _EMPTY_SUPPRESS
        print(f"livemap ladd suppress refresh kept current: {type(exc).__name__}: {exc}", flush=True)
        return current
    _cache_ladd_suppress(fresh)   # persist last-good so a cold-start restart reseeds instead of failing open
    return fresh


# value shape: ip -> (tokens, last_seen_ts)
_rate_buckets: collections.OrderedDict = collections.OrderedDict()


def _client_ip(request) -> str:
    # Only Cloudflare can reach the public instance (tunnel-only ingress), so CF-Connecting-IP is the real
    # client; fall back to the socket peer for a direct/local hit.
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_allow(key, now, buckets, burst=RATE_LIMIT_BURST,
                      refill=RATE_LIMIT_REFILL_PER_SEC, max_buckets=RATE_BUCKETS_MAX) -> bool:
    tokens, last = buckets.pop(key, (burst, now))   # pop+reinsert = move-to-end, so dict order IS recency
    tokens = min(burst, tokens + (now - last) * refill)
    allowed = tokens >= 1.0
    buckets[key] = (tokens - 1.0 if allowed else tokens, now)
    # Hard LRU ceiling, O(1) per request: evicting the least-recently-seen bucket only re-grants that IP a
    # fresh burst — acceptable for a backstop limiter, unlike unbounded growth plus a full-map scan per
    # request under exactly the distributed load the limiter exists to absorb.
    while len(buckets) > max_buckets:
        del buckets[next(iter(buckets))]
    return allowed


async def _poller() -> None:
    global _snapshot, _outline, _routes, _ladd_suppress
    n = 0
    while True:
        try:
            # refresh the suppression set before _fetch so even the first snapshot is already filtered
            if _should_refresh_ladd(_ladd_suppress, n):
                _ladd_suppress = await asyncio.to_thread(_refresh_ladd_suppress, _ladd_suppress)
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


if PUBLIC_MODE:
    # Public hardening: per-IP rate limit on the DB endpoints, an edge-cache hint on /aircraft, and security
    # headers. Registered only in public mode so the private instance's responses stay byte-identical.
    @app.middleware("http")
    async def _public_hardening(request, call_next):
        path = request.url.path
        if path.startswith(RATE_LIMITED_PREFIXES) and not _rate_limit_allow(
            # monotonic, not wall-clock: an NTP step backwards would make elapsed negative and over-deny
            _client_ip(request), time.monotonic(), _rate_buckets
        ):
            # Generic body: the limit is rate-based, not identity-based, so a 429 can't be a privacy oracle.
            resp = JSONResponse({"detail": "rate limited"}, status_code=429)
        else:
            resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        if path == "/aircraft":
            # s-maxage=1: with a dashboard Cache Rule making /aircraft cache-eligible (Cloudflare doesn't
            # cache JSON by default), the edge absorbs the snapshot fan-out; browsers still poll ~live.
            resp.headers["Cache-Control"] = "public, s-maxage=1"
        elif path.startswith("/path/"):
            # rate-limit 429s bypass the endpoint's envelope — keep the every-response no-store belt intact
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp


@app.get("/aircraft")
async def aircraft() -> JSONResponse:
    return JSONResponse(_snapshot)


@app.get("/range-outline")
async def range_outline() -> JSONResponse:
    # Public mode serves no receiver anchor (center null): the public-landmark default was judged too close
    # to the real antenna to anchor publicly. Frontend null-guards marker/rings/range-bearing; ring renders.
    center = None if PUBLIC_MODE else [FEEDER_LON, FEEDER_LAT]
    return JSONResponse({"center": center, "ring": _outline})


@app.get("/track/{icao}")
async def track(icao: str) -> JSONResponse:
    # None state = suppression never loaded → fail closed for ALL hexes (this surface has no MV belt of its own).
    if _ladd_suppress is None:
        return JSONResponse({"hex": icao, "points": []})
    # LADD: a currently-listed hex returns empty — indistinguishable from "no track", so no privacy oracle.
    # Honor the dim identity set AND the live MV belt (mv_track_positions carries no dbFlags bit).
    if _is_ladd_suppressed(icao, None, mv_is_ladd=False, suppress=_ladd_suppress) \
            or _track_belt_suppressed(icao, time.time(), _mv_ladd_hexes):
        return JSONResponse({"hex": icao, "points": []})
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
    # Live-set LADD gate runs per-request around the CH cache (the mart's is_ladd is batch-refreshed): a newly
    # listed airframe would else keep serving history here while /aircraft, /track, /path already suppress it.
    suppress = _ladd_suppress
    # None = suppression never loaded → fail closed for every hex (mirrors /track); empty is indistinguishable
    # from no-history, so no privacy oracle.
    if suppress is None:
        return JSONResponse({"hex": hex, "flights": []})
    # Requested hex listed right now → empty before we read cache or touch CH, warm cache or not.
    if _is_ladd_suppressed(key, None, mv_is_ladd=False, suppress=suppress):
        return JSONResponse({"hex": hex, "flights": []})
    now = time.time()
    hit = _flights_cache.get(key)
    if hit and hit[0] > now:
        return JSONResponse({"hex": hex, "flights": _ladd_filter_flights(hit[1], suppress)})
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
    return JSONResponse({"hex": hex, "flights": _ladd_filter_flights(rows, suppress)})


@app.get("/path/{flight_id}")
async def path(flight_id: str) -> JSONResponse:
    # Suppressed / unknown / missing-table / CH-down all return the same empty shape — never 404/500, no
    # privacy oracle; "provisional" rides every response so the flag itself leaks nothing.
    if not _valid_flight_id(flight_id):
        return _path_response(flight_id, [], False)
    suppress = _ladd_suppress
    if suppress is None:
        return _path_response(flight_id, [], False)
    try:
        auth = await asyncio.to_thread(_fetch_path_auth, flight_id)
    except Exception as exc:
        print(f"livemap path auth failed: {type(exc).__name__}: {exc}", flush=True)
        return _path_response(flight_id, [], False)
    if auth is None:
        return _path_response(flight_id, [], False)
    icao24, callsign, mart_ladd, start_s, end_s, start_day = auth
    if _is_ladd_suppressed(icao24, callsign, mv_is_ladd=mart_ladd, suppress=suppress):
        return _path_response(flight_id, [], False)
    fid = int(flight_id)  # canonical cache key: leading-zero aliases must not mint distinct entries
    now = time.time()
    hit = _path_cache.get(fid)
    cached_empty = bool(hit) and hit[0] > now and not hit[1]
    if hit and hit[0] > now and hit[1]:
        return _path_response(flight_id, hit[1], False)
    if not cached_empty:
        try:
            points = await asyncio.to_thread(_fetch_path, flight_id)
        except Exception as exc:
            print(f"livemap path fetch failed: {type(exc).__name__}: {exc}", flush=True)
            return _path_response(flight_id, [], False)
        if points:
            _path_cache_put(fid, points, now)  # immutable settled-day history: cache non-empty successes
            return _path_response(flight_id, points, False)
    # settled empty → eligibility gate: an empty cache HIT classifies-and-continues, it never short-circuits
    head = await asyncio.to_thread(_get_path_head, now)
    if head is None or start_day is None:  # cold head fetch failed / windowless spine row → fail closed
        return _path_response(flight_id, [], False)
    if start_day <= head:
        # historical pathless keeps today's behavior; the empty is cacheable only now that it's classified
        if not cached_empty:
            _path_cache_put(fid, [], now)
        return _path_response(flight_id, [], False)
    if start_s is None or end_s is None:
        return _path_response(flight_id, [], False)
    try:
        points = await asyncio.to_thread(_fetch_provisional, fid, icao24, start_s, end_s)
    except Exception as exc:
        print(f"livemap provisional path failed: {type(exc).__name__}: {exc}", flush=True)
        return _path_response(flight_id, [], False)
    # bypasses _path_cache: bronze grows all day and the flight settles within days — a 900 s entry is
    # wrong in both directions and the recompute is cheap (~114 ms worst-case measured)
    return _path_response(flight_id, points, True)


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
