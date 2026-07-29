import asyncio
import datetime
import os

CH_DB = os.environ.get("LIVEMAP_CH_DB", "gold_ch")

# Provisional fallback (rung 2): flights newer than the mart's built head get a serve-time fused path.
# 600 s mirrors dbt path_window_pad_min: 10 — mart windows are observation clips, 5.97% of path rows are pad-only.
WINDOW_PAD_S = 600
HEAD_QUERY = f"SELECT max(day_key) FROM {CH_DB}.fct_flight_path"
HEAD_TTL_S = float(os.environ.get("LIVEMAP_PATH_HEAD_TTL_S", "60"))

# Overlapping same-hex windows contest fixes in-process (mart stage-1 rule); deliberately is_ladd-blind — a
# suppressed neighbor still wins its own fixes away. ov_lo/ov_hi pre-bake ±pad on both windows (2·pad).
COMPETITOR_QUERY = f"""
    SELECT flight_id, toUnixTimestamp(start_time), toUnixTimestamp(end_time)
    FROM {CH_DB}.fct_flights_reconciled
    WHERE lower(icao24) = {{hex:String}} AND flight_id != {{fid:UInt64}}
      AND start_time <= toDateTime({{ov_hi:Int64}}) AND end_time >= toDateTime({{ov_lo:Int64}})
"""

# The three bronze scans mirror fct_flight_path's fixes CTEs (timestamp choice, units, sentinels) with
# physical leading-key predicates; uniform projection (ts_s, lat, lon, alt_ft, on_ground, gs_kt, track_deg).
ADSB_QUERY = """
    SELECT toUInt32(floor(assumeNotNull(capture_ts))), lat, lon,
           if(alt_baro = 'ground', 0, toFloat64OrNull(alt_baro)),
           toUInt8(coalesce(alt_baro, '') = 'ground'), gs, track
    FROM bronze.adsb_states
    WHERE capture_date BETWEEN {day_lo:Date} AND {day_hi:Date}
      AND lower(hex) = {hex:String}
      AND capture_ts IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL
      AND capture_ts >= {win_lo:Int64} AND capture_ts < {win_hi:Int64} + 1
"""

ADSBLOL_QUERY = """
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
OPENSKY_QUERY = """
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


def contest_keep(ts, fid, start_s, end_s, competitors) -> bool:
    # Mart stage-1 in-process: each fix goes to the nearest unpadded-window midpoint among PADDED windows
    # containing it, flight_id tiebreak — a total order, so no ordering ambiguity can flip a winner.
    best = (abs(ts - (start_s + end_s) // 2), fid)
    for cfid, cs, ce in competitors:
        if cs - WINDOW_PAD_S <= ts <= ce + WINDOW_PAD_S:
            cand = (abs(ts - (cs + ce) // 2), cfid)
            if cand < best:
                best = cand
    return best[1] == fid


def fuse_points(rows) -> list:
    # Mart stage-2: one row per whole second by src_rank then full-tuple total order, NULLS LAST like CH —
    # a naive tuple sort mixing None and float raises, so nullable fields sort as (is None, value).
    def key(r):
        ts, rank, lat, lon, alt, gs, trk, og, _src = r
        return (ts, rank, lat, lon, (alt is None, alt), (gs is None, gs), (trk is None, trk), og)

    out, last_ts = [], None
    for ts, _rank, lat, lon, alt, gs, trk, og, src in sorted(rows, key=key):
        if ts == last_ts:
            continue
        last_ts = ts
        out.append((ts, lat, lon, alt, int(og or 0), gs, trk, src))
    return out


def fetch_provisional(ch_client, fid, icao24, start_s, end_s):
    # Raises on ANY stage failure — the endpoint catches to empty. A partial fusion must never masquerade as
    # a complete path, and a failed competitor lookup must never read as "zero competitors".
    win_lo, win_hi = start_s - WINDOW_PAD_S, end_s + WINDOW_PAD_S
    utc = datetime.timezone.utc
    day_lo = datetime.datetime.fromtimestamp(win_lo, tz=utc).date()
    day_hi = datetime.datetime.fromtimestamp(win_hi, tz=utc).date()
    one = datetime.timedelta(days=1)

    def day_epoch(d):
        return int(datetime.datetime.combine(d, datetime.time(), tzinfo=utc).timestamp())

    client = ch_client()
    try:
        comp = client.query(COMPETITOR_QUERY, parameters={
            "hex": icao24, "fid": fid,
            "ov_lo": start_s - 2 * WINDOW_PAD_S, "ov_hi": end_s + 2 * WINDOW_PAD_S,
        }).result_rows
        competitors = [(int(c), int(s), int(e)) for c, s, e in comp]
        window = {"hex": icao24, "win_lo": win_lo, "win_hi": win_hi}
        rows = []
        for src, rank, query, params in (
            ("adsb", 1, ADSB_QUERY, {"day_lo": day_lo, "day_hi": day_hi}),
            ("adsblol", 2, ADSBLOL_QUERY, {"halo_lo": day_lo - one, "halo_hi": day_hi + one}),
            ("opensky", 3, OPENSKY_QUERY,
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
    return fuse_points([r for r in rows if contest_keep(r[0], fid, start_s, end_s, competitors)])


def fetch_head(ch_client):
    client = ch_client()
    try:
        res = client.query(HEAD_QUERY)
    finally:
        client.close()
    head = res.result_rows[0][0] if res.result_rows else None
    # empty mart (max → NULL): everything is post-head — the fallback serves until the first build lands
    return head or datetime.date(1970, 1, 1)


async def get_head(now, state, fetch):
    if state["head"] is not None and state["expiry"] > now:
        return state["head"]
    try:
        head = await asyncio.to_thread(fetch)
    except Exception as exc:
        print(f"livemap path head fetch failed: {type(exc).__name__}: {exc}", flush=True)
        return state["head"]
    state["expiry"] = now + HEAD_TTL_S
    state["head"] = head
    return head
