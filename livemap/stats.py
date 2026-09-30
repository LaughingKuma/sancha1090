import asyncio
import datetime
import time

STATS_CONTRACT = 1
STATS_MIN_AIRFRAMES = 3
# A dim_ladd that exists but is empty or truncated must not pass for a loaded one (70k open rows today).
STATS_MIN_LADD_OPEN = 1000
FRESH_TTL_S = 600
DATABASE_STALE_MAX_S = 3600
TOP_N = 15
RAREST_N = 10
LIST_ROWS_MAX = 500
NMI_KM = 1.852

# One list per fill so every section drops the same airframes; a NULL is_ladd is listed, failing closed.
# The cuts are scalar subqueries, read before the arms, so the list always covers everything below them.
EXCLUSION_QUERY = """
WITH ladd_hex AS (SELECT lower(trimBoth(icao24)) FROM dim.dim_ladd FINAL
                  WHERE valid_to IS NULL AND icao24 IS NOT NULL),
     ladd_cs AS (SELECT upper(trimBoth(callsign)) FROM dim.dim_ladd FINAL
                 WHERE valid_to IS NULL AND callsign IS NOT NULL),
     roof_f AS (SELECT flight_id FROM gold_ch.fct_flight_path_summary WHERE n_adsb > 0)
SELECT groupUniqArray(h) AS bad,
       (SELECT count() FROM dim.dim_ladd FINAL WHERE valid_to IS NULL) AS n_open,
       (SELECT toUnixTimestamp(max(hour_utc)) FROM gold_ch.agg_rooftop_airframe_hour) AS roof_cut,
       (SELECT max(day_key) FROM gold_ch.fct_flight_path_summary) AS path_cut
FROM (SELECT hex AS h FROM gold_ch.agg_rooftop_airframe_hour WHERE flag_ladd = 1
      UNION DISTINCT SELECT hex FROM gold_ch.agg_rooftop_airframe_hour ARRAY JOIN callsigns AS c
        WHERE c IN ladd_cs
      UNION DISTINCT SELECT hex FROM gold_ch.agg_rooftop_airframe_hour WHERE hex IN ladd_hex
      UNION DISTINCT SELECT lower(r.icao24) FROM gold_ch.fct_flights_reconciled AS r
        WHERE r.flight_id IN roof_f
          AND (ifNull(r.is_ladd, 1) = 1 OR lower(r.icao24) IN ladd_hex
               OR upper(trimBoth(ifNull(r.callsign, ''))) IN ladd_cs))
"""

# Strictly below X1's newest hour: it is still filling, and a rebuild can add airframes X1 never listed.
ROOF_CUT = "hour_utc < toDateTime({roof_cut:UInt32}, 'UTC')"

_PER_AIRFRAME = f"""
    SELECT hex, argMaxIf(typecode, hour_utc, ifNull(typecode, '') != '') AS tc,
           max(flag_heli) AS heli, max(flag_mil) AS mil, min(capture_date) AS first_day,
           max(alt_rec_ft) AS alt_ft, max(gs_rec_kt) AS gs_kt, max(dst_rec_nmi) AS dst_nmi
    FROM gold_ch.agg_rooftop_airframe_hour
    WHERE hex NOT IN {{bad:Array(String)}} AND {ROOF_CUT}
    GROUP BY hex"""

# The LEFT JOIN yields '' not NULL for a missing type under the client's join_use_nulls=0.
TYPES_QUERY = f"""
SELECT a.tc AS typecode, nullIf(ty.model_name, '') AS model, nullIf(ty.body_class, '') AS body_class,
       uniqExact(a.hex) AS airframes, uniqExactIf(a.hex, a.heli = 1) AS heli_airframes
FROM ({_PER_AIRFRAME}) AS a
LEFT ANY JOIN silver_ch.dim_aircraft_types AS ty ON ty.typecode = a.tc
WHERE ifNull(a.tc, '') != ''
GROUP BY typecode, model, body_class
ORDER BY airframes DESC, typecode
"""

# tuple(tc) keeps a NULL-typed record holder instead of argMax skipping to another airframe's type.
RECORDS_QUERY = f"""
SELECT count() AS airframes, countIf(mil = 1) AS military, min(first_day) AS since,
       max(alt_ft) AS highest_ft, argMaxIf(tuple(tc), (alt_ft, hex), alt_ft IS NOT NULL).1 AS alt_tc,
       max(gs_kt) AS fastest_kt, argMaxIf(tuple(tc), (gs_kt, hex), gs_kt IS NOT NULL).1 AS gs_tc,
       max(dst_nmi) AS farthest_nmi
FROM ({_PER_AIRFRAME})
"""

HOURLY_QUERY = f"""
SELECT toDate(hour_utc, 'Asia/Tokyo') AS day, toHour(hour_utc, 'Asia/Tokyo') AS hour,
       uniqExact(hex) AS aircraft
FROM gold_ch.agg_rooftop_airframe_hour
WHERE hex NOT IN {{bad:Array(String)}} AND {ROOF_CUT}
GROUP BY day, hour
"""

PEAK_MINUTE_QUERY = f"""
SELECT toUnixTimestamp(hour_utc + toIntervalMinute(m)) AS at, uniqExact(hex) AS aircraft
FROM gold_ch.agg_rooftop_airframe_hour ARRAY JOIN minutes AS m
WHERE hex NOT IN {{bad:Array(String)}} AND {ROOF_CUT}
GROUP BY at
ORDER BY aircraft DESC, at
LIMIT 1
"""

# The summary bounds new flights, not the mart: the mart rebuilds whole every 30 min up to now, while a flight
# enters the summary only when its settled UTC day lands, so the newest day_key X1 saw is a hard edge.
_ROOF_FLIGHTS = """
FROM gold_ch.fct_flights_reconciled AS r
WHERE r.flight_id IN (SELECT flight_id FROM gold_ch.fct_flight_path_summary
                      WHERE n_adsb > 0 AND day_key <= {path_cut:Date})
  AND r.icao24 IS NOT NULL AND lower(r.icao24) NOT IN {bad:Array(String)}"""

AIRLINES_QUERY = f"""
SELECT r.airline_name AS name, count() AS flights, uniqExact(lower(r.icao24)) AS airframes
{_ROOF_FLIGHTS}
  AND ifNull(r.airline_name, '') != ''
GROUP BY name
ORDER BY flights DESC, name
LIMIT {LIST_ROWS_MAX}
"""

ROUTES_QUERY = f"""
SELECT coalesce(nullIf(r.origin_iata, ''), r.origin_icao) AS o,
       coalesce(nullIf(r.dest_iata, ''), r.dest_icao) AS d,
       min(r.origin_city) AS o_city, min(r.dest_city) AS d_city,
       count() AS flights, uniqExact(lower(r.icao24)) AS airframes
{_ROOF_FLIGHTS}
  AND ifNull(o, '') != '' AND ifNull(d, '') != ''
GROUP BY o, d
ORDER BY flights DESC, o, d
LIMIT {LIST_ROWS_MAX}
"""

DAILY_FLIGHTS_QUERY = f"""
SELECT assumeNotNull(toDate(r.start_time, 'Asia/Tokyo')) AS day, count() AS flights
{_ROOF_FLIGHTS}
  AND r.start_time IS NOT NULL
GROUP BY day
ORDER BY day
"""

ROOFTOP_QUERIES = (TYPES_QUERY, RECORDS_QUERY, HOURLY_QUERY, PEAK_MINUTE_QUERY,
                   AIRLINES_QUERY, ROUTES_QUERY, DAILY_FLIGHTS_QUERY)

LAYERS = {"bronze": "bronze", "silver_ch": "silver", "gold_ch": "gold", "dim": "dim"}
LANES = (("rooftop", "adsb_states"), ("opensky", "opensky_states"),
         ("adsb.lol", "adsblol_flight_segments"), ("swim", "swim_flightdata"))

PARTS_QUERY = """
SELECT database, table, sum(rows) AS rows, sum(bytes_on_disk) AS bytes,
       sum(data_uncompressed_bytes) AS raw_bytes
FROM system.parts
WHERE active AND database IN ('bronze', 'silver_ch', 'gold_ch', 'dim')
GROUP BY database, table
"""

# The newest two days of each lane only; max() of a partition-key date reads part metadata.
FRESHNESS_QUERY = """
SELECT 'rooftop' AS lane, toUInt32(max(capture_ts)) AS latest FROM bronze.adsb_states
WHERE capture_date >= (SELECT max(capture_date) FROM bronze.adsb_states) - 1
UNION ALL
SELECT 'opensky', toUnixTimestamp(max(snapshot_time)) FROM bronze.opensky_states
WHERE snapshot_date >= (SELECT max(snapshot_date) FROM bronze.opensky_states) - 1
UNION ALL
SELECT 'adsb.lol', toUnixTimestamp(max(seg_end)) FROM bronze.adsblol_flight_segments
WHERE trace_day >= (SELECT max(trace_day) FROM bronze.adsblol_flight_segments) - 1
UNION ALL
SELECT 'swim', toUnixTimestamp(max(msg_timestamp)) FROM bronze.swim_flightdata
WHERE swim_date >= (SELECT max(swim_date) FROM bronze.swim_flightdata) - 1
"""

ROWS_PER_DAY_QUERY = """
SELECT 'rooftop' AS lane, capture_date AS day, count() AS rows FROM bronze.adsb_states GROUP BY day
UNION ALL
SELECT 'opensky', snapshot_date, count() FROM bronze.opensky_states GROUP BY snapshot_date
UNION ALL
SELECT 'adsb.lol', trace_day, count() FROM bronze.adsblol_flight_segments GROUP BY trace_day
UNION ALL
SELECT 'swim', swim_date, count() FROM bronze.swim_flightdata GROUP BY swim_date
"""

# messages is readsb's per-airframe running counter, so a day's traffic is its per-hex spread.
MESSAGES_QUERY = """
SELECT day, sum(spread) AS messages
FROM (SELECT capture_date AS day, max(messages) - min(messages) AS spread
      FROM bronze.adsb_states
      WHERE capture_date >= today() - 30 AND hex IS NOT NULL AND messages IS NOT NULL
      GROUP BY day, hex)
GROUP BY day
ORDER BY day
"""

DATABASE_QUERIES = (PARTS_QUERY, FRESHNESS_QUERY, ROWS_PER_DAY_QUERY, MESSAGES_QUERY)


class Unavailable(Exception):
    pass


def rows(client, sql, params=None) -> list:
    res = client.query(sql, parameters=params)
    return [dict(zip(res.column_names, r, strict=True)) for r in res.result_rows]


def passes_floor(row) -> bool:
    # The single place the 3-airframe floor is applied to any named type, airline or route.
    return int(row.get("airframes") or 0) >= STATS_MIN_AIRFRAMES


def exclusion_params(x1_rows) -> dict:
    if len(x1_rows) != 1:
        raise Unavailable("exclusion query returned no row")
    x1 = x1_rows[0]
    n_open = int(x1.get("n_open") or 0)
    if n_open < STATS_MIN_LADD_OPEN:
        raise Unavailable(f"dim_ladd has {n_open} open rows")
    if x1.get("roof_cut") is None or x1.get("path_cut") is None:
        raise Unavailable("exclusion query returned no cut")
    return {"bad": sorted({str(h) for h in x1.get("bad") or () if h}),
            "roof_cut": int(x1["roof_cut"]), "path_cut": x1["path_cut"]}


def _day(v) -> str | None:
    return v.isoformat() if isinstance(v, datetime.date) else (str(v) if v is not None else None)


def _num(v, nd=None):
    if v is None:
        return None
    return round(float(v), nd) if nd else int(round(float(v)))


def _type_row(r) -> dict:
    n = int(r["airframes"])
    return {"typecode": r["typecode"], "model": r.get("model") or None,
            "body_class": r.get("body_class") or None,
            "is_helicopter": int(r.get("heli_airframes") or 0) * 2 > n, "airframes": n}


def assemble_types(type_rows) -> tuple:
    shown = [r for r in type_rows if passes_floor(r)]
    top = sorted(shown, key=lambda r: (-int(r["airframes"]), r["typecode"]))[:TOP_N]
    rare = sorted(shown, key=lambda r: (int(r["airframes"]), r["typecode"]))[:RAREST_N]
    return ([_type_row(r) for r in top], [_type_row(r) for r in rare],
            len(type_rows) - len(shown), {r["typecode"] for r in shown})


def hour_of_day(hourly_rows) -> list:
    # First and last days are partial (capture start, today so far), so they would drag the average down.
    days = sorted({r["day"] for r in hourly_rows})
    keep = set(days[1:-1]) if len(days) > 2 else set(days)
    sums = [0] * 24
    for r in hourly_rows:
        if r["day"] in keep:
            sums[int(r["hour"])] += int(r["aircraft"])
    n = len(keep) or 1
    return [{"hour": h, "avg_aircraft": round(sums[h] / n, 1)} for h in range(24)]


def assemble_rooftop(type_rows, rec_rows, hourly, peak, airlines, routes, daily) -> dict:
    types, rarest, below, shown_types = assemble_types(type_rows)
    rec = rec_rows[0] if rec_rows else {}
    # The first day starts mid-capture and the newest JST day runs past the summary cut, so neither is whole.
    per_day = [{"day": _day(r["day"]), "flights": int(r["flights"])} for r in daily][1:-1]
    busiest = min(per_day, key=lambda r: (-r["flights"], r["day"]), default=None)
    nmi = _num(rec.get("farthest_nmi"))
    return {
        "since": _day(rec.get("since")),
        "airframes": int(rec.get("airframes") or 0),
        "types": types, "rarest_types": rarest, "types_below_floor": below,
        "airlines": [{"name": r["name"], "flights": int(r["flights"]), "airframes": int(r["airframes"])}
                     for r in airlines if passes_floor(r)][:TOP_N],
        "routes": [{"o": r["o"], "o_city": r.get("o_city") or None, "d": r["d"],
                    "d_city": r.get("d_city") or None, "flights": int(r["flights"])}
                   for r in routes if passes_floor(r)][:TOP_N],
        "routes_as_of": per_day[-1]["day"] if per_day else None,
        "hour_of_day": hour_of_day(hourly),
        "per_day": per_day,
        "busiest_day": busiest,
        "peak_minute": ({"at": int(peak[0]["at"]), "aircraft": int(peak[0]["aircraft"])} if peak else None),
        "records": {
            "highest_ft": {"value": _num(rec.get("highest_ft")),
                           "typecode": rec.get("alt_tc") if rec.get("alt_tc") in shown_types else None},
            "fastest_kt": {"value": _num(rec.get("fastest_kt"), 1),
                           "typecode": rec.get("gs_tc") if rec.get("gs_tc") in shown_types else None},
            "farthest_nmi": nmi,
            "farthest_km": _num(float(rec["farthest_nmi"]) * NMI_KM) if nmi is not None else None,
        },
        "military_airframes": int(rec.get("military") or 0),
    }


def compute_rooftop(client_factory) -> dict:
    client = client_factory()
    try:
        params = exclusion_params(rows(client, EXCLUSION_QUERY))
        section = assemble_rooftop(*(rows(client, q, params) for q in ROOFTOP_QUERIES))
        # A rebuild mid-fill can land late bronze below the cuts; a newly listed airframe voids the fill.
        if set(exclusion_params(rows(client, EXCLUSION_QUERY))["bad"]) - set(params["bad"]):
            raise Unavailable("exclusion list grew during the fill")
        return section
    finally:
        client.close()


def assemble_database(parts, fresh, per_day_rows, messages, today) -> dict:
    layers = {}
    for r in parts:
        name = LAYERS.get(r["database"])
        if name is None:
            continue
        acc = layers.setdefault(name, [0, 0, 0])
        for i, k in enumerate(("rows", "bytes", "raw_bytes")):
            acc[i] += int(r[k] or 0)
    bronze = {r["table"]: r for r in parts if r["database"] == "bronze"}
    lo = today - datetime.timedelta(days=30)
    lanes = []
    for lane, table in LANES:
        days = sorted(((r["day"], int(r["rows"])) for r in per_day_rows if r["lane"] == lane))
        recent = [(d, n) for d, n in days if lo <= d < today]
        avg = sum(n for _, n in recent) / len(recent) if recent else None
        peak = max(recent, key=lambda x: (x[1], x[0]), default=None)
        t = bronze.get(table)
        per_row = int(t["bytes"]) / int(t["rows"]) if t and int(t["rows"] or 0) else None
        lanes.append({
            "lane": lane, "days": [{"day": _day(d), "rows": n} for d, n in days],
            "avg_30d": _num(avg), "peak_30d": {"day": _day(peak[0]), "rows": peak[1]} if peak else None,
            "bytes_per_day": _num(avg * per_row) if avg is not None and per_row else None,
        })
    order = list(LAYERS.values())
    latest = {r["lane"]: r["latest"] for r in fresh}
    return {
        "layers": [{"layer": k, "rows": v[0], "bytes": v[1], "raw_bytes": v[2],
                    "ratio": round(v[2] / v[1], 1) if v[1] else None}
                   for k, v in sorted(layers.items(), key=lambda kv: order.index(kv[0]))],
        "freshness": [{"lane": lane, "latest": _num(latest.get(lane))} for lane, _ in LANES],
        "per_day": lanes,
        "messages_per_day": [{"day": _day(r["day"]), "messages": int(r["messages"])} for r in messages],
    }


def compute_database(client_factory) -> dict:
    client = client_factory()
    try:
        got = [rows(client, q) for q in DATABASE_QUERIES]
    finally:
        client.close()
    return assemble_database(*got, datetime.datetime.now(datetime.timezone.utc).date())


class StatsCache:
    # Concurrent misses share one refill, so a cold burst never runs the heavy queries once per request.
    def __init__(self, put, clock=time.time, mono=time.monotonic):
        self.put = put
        self.clock = clock
        # expiry and staleness run on mono: a wall-clock step back must never extend an entry's life
        self.mono = mono
        self.entries: dict = {}
        self._inflight: dict = {}

    async def _refill(self, key, compute):
        as_of, started = self.clock(), self.mono()
        try:
            payload = await asyncio.to_thread(compute)
        except Exception as exc:
            print(f"livemap stats {key} failed: {type(exc).__name__}: {exc}", flush=True)
            return None
        # expiry counts from before the reads, so no entry outlives FRESH_TTL_S past its X1
        self.put(self.entries, key, (started + FRESH_TTL_S, payload, as_of, started), started, 8)
        return payload

    def seconds_left(self, key) -> int:
        hit = self.entries.get(key)
        return max(0, int(hit[0] - self.mono())) if hit else 0

    async def _single_flight(self, key, compute):
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._refill(key, compute))
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        return await asyncio.shield(task)

    async def section(self, key, compute, stale_max_s=0) -> dict:
        hit = self.entries.get(key)
        if hit and hit[0] > self.mono():
            return {"status": "ok", "as_of": int(hit[2]), **hit[1]}
        await self._single_flight(key, compute)
        hit = self.entries.get(key)
        now = self.mono()
        age = now - hit[3] if hit else None
        if age is not None and hit[0] > now:
            return {"status": "ok", "as_of": int(hit[2]), **hit[1]}
        if age is not None and age <= stale_max_s:
            return {"status": "ok", "stale": True, "as_of": int(hit[2]), **hit[1]}
        return {"status": "unavailable"}
