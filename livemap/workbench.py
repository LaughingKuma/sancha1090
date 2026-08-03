import datetime
import os

# Config-free like pathfusion.py — the schema knob is read once here, not threaded from app.py.
CH_DB = os.environ.get("LIVEMAP_CH_DB", "gold_ch")
TIER_TBL = f"{CH_DB}.fct_flight_recon_tier"
RECON_TBL = f"{CH_DB}.fct_flights_reconciled"

# Sentinel-guarded ANDs: every optional filter is always bound (never conditionally interpolated),
# an empty/wide sentinel just makes the clause vacuously true. Shared by main/count/od-breakdown.
_INSTANCES_WHERE_NO_OD = (
    "({callsign:String} = '' OR upper(trimBoth(r.callsign)) = {callsign:String}) "
    "AND ({airline:String} = '' OR r.airline_name = {airline:String}) "
    "AND ({hex:String} = '' OR lower(r.icao24) = {hex:String}) "
    "AND ({reg:String} = '' OR upper(r.registration) = {reg:String}) "
    "AND ({airport:String} = '' OR {airport:String} IN "
    "(upper(r.origin_icao), upper(r.origin_iata), upper(r.dest_icao), upper(r.dest_iata))) "
    "AND ({type:String} = '' OR r.typecode = {type:String}) "
    "AND toDate(r.start_time, 'Asia/Tokyo') BETWEEN {day_from:Date} AND {day_to:Date}"
)
_INSTANCES_WHERE = (
    _INSTANCES_WHERE_NO_OD
    + " AND ({od_o:String} = '' OR coalesce(r.origin_iata, r.origin_icao) = {od_o:String})"
    + " AND ({od_d:String} = '' OR coalesce(r.dest_iata, r.dest_icao) = {od_d:String})"
)
# Military only ever runs against the tier variant — the no-tier fallback never carries this clause.
_MIL_CLAUSE = " AND ({military:UInt8} = 0 OR t.is_military = 1)"

_INSTANCES_SELECT = (
    "SELECT toString(r.flight_id) AS flight_id, "
    "toString(toDate(r.start_time, 'Asia/Tokyo')) AS day, "
    "r.start_time AS start_time, r.end_time AS end_time, "
    "lower(r.icao24) AS icao24, r.registration AS registration, r.typecode AS typecode, "
    "r.callsign AS callsign, r.airline_name AS airline, "
    "r.origin_icao AS origin_icao, r.origin_iata AS origin_iata, r.origin_city AS origin_city, "
    "r.dest_icao AS dest_icao, r.dest_iata AS dest_iata, r.dest_city AS dest_city, "
)
# join_use_nulls is off, so a LEFT JOIN miss arrives as '' (never NULL) — an unmatched tier row
# must be caught by both, or a lagging tier mart would serve a tier outside the documented enum.
_INSTANCES_TIER_COLS = (
    "if(coalesce(t.tier, '') = '', 'unknown', t.tier) AS tier, t.effective_gap_s AS effective_gap_s, "
    "t.n_points AS n_points, coalesce(t.is_military, 0) AS is_military "
)
_INSTANCES_NO_TIER_COLS = (
    "'unknown' AS tier, NULL AS effective_gap_s, NULL AS n_points, 0 AS is_military "
)
_TIER_JOIN = f"FROM {RECON_TBL} r LEFT JOIN {TIER_TBL} t ON t.flight_id = r.flight_id WHERE "
_NO_TIER_FROM = f"FROM {RECON_TBL} r WHERE "
# Two CH 26.5 optimizer defects on this exact shape: lazy materialization mis-resolves the tier
# LEFT JOIN's block, and topK dynamic filtering overflows on the Nullable(DateTime64) sort key.
INSTANCES_QUERY_SETTINGS = {"query_plan_optimize_lazy_materialization": 0,
                            "use_top_k_dynamic_filtering": 0}

# start_time alone is not a total order (19k+ tied timestamps measured), so LIMIT/OFFSET pages would
# duplicate rows and hide others — flight_id breaks every tie deterministically.
_PAGE_DESC = " ORDER BY r.start_time DESC, r.flight_id DESC LIMIT {limit:UInt64} OFFSET {offset:UInt64}"
_PAGE_ASC = " ORDER BY r.start_time ASC, r.flight_id ASC LIMIT {limit:UInt64} OFFSET {offset:UInt64}"

INSTANCES_QUERY_TIER_DESC = (
    _INSTANCES_SELECT + _INSTANCES_TIER_COLS + _TIER_JOIN + _INSTANCES_WHERE + _MIL_CLAUSE + _PAGE_DESC
)
INSTANCES_QUERY_TIER_ASC = (
    _INSTANCES_SELECT + _INSTANCES_TIER_COLS + _TIER_JOIN + _INSTANCES_WHERE + _MIL_CLAUSE + _PAGE_ASC
)
INSTANCES_QUERY_NO_TIER_DESC = (
    _INSTANCES_SELECT + _INSTANCES_NO_TIER_COLS + _NO_TIER_FROM + _INSTANCES_WHERE + _PAGE_DESC
)
INSTANCES_QUERY_NO_TIER_ASC = (
    _INSTANCES_SELECT + _INSTANCES_NO_TIER_COLS + _NO_TIER_FROM + _INSTANCES_WHERE + _PAGE_ASC
)
INSTANCES_COUNT_QUERY_TIER = (
    "SELECT count() " + _TIER_JOIN + _INSTANCES_WHERE + _MIL_CLAUSE
)
INSTANCES_COUNT_QUERY_NO_TIER = (
    "SELECT count() " + _NO_TIER_FROM + _INSTANCES_WHERE
)
_OD_BREAKDOWN_SELECT = (
    "SELECT coalesce(r.origin_iata, r.origin_icao) AS o, coalesce(r.dest_iata, r.dest_icao) AS d, count() AS n "
)
_OD_BREAKDOWN_TAIL = (
    "AND coalesce(r.origin_iata, r.origin_icao) IS NOT NULL AND coalesce(r.dest_iata, r.dest_icao) IS NOT NULL "
    "GROUP BY o, d ORDER BY n DESC, o, d LIMIT 8"
)
INSTANCES_OD_BREAKDOWN_QUERY_TIER = (
    _OD_BREAKDOWN_SELECT + _TIER_JOIN + _INSTANCES_WHERE_NO_OD + _MIL_CLAUSE + " " + _OD_BREAKDOWN_TAIL
)
INSTANCES_OD_BREAKDOWN_QUERY_NO_TIER = (
    _OD_BREAKDOWN_SELECT + _NO_TIER_FROM + _INSTANCES_WHERE_NO_OD + " " + _OD_BREAKDOWN_TAIL
)

AIRLINES_QUERY_TIER = (
    "SELECT r.airline_name AS name, count() AS n_flights, uniqExact(r.callsign) AS n_services, "
    "toString(min(toDate(r.start_time, 'Asia/Tokyo'))) AS first_day, "
    "toString(max(toDate(r.start_time, 'Asia/Tokyo'))) AS last_day, "
    "countIf(t.tier = 'settled') AS tier_settled, countIf(t.tier = 'estimated') AS tier_estimated, "
    "countIf(t.tier = 'provisional') AS tier_provisional, "
    "countIf(coalesce(t.tier, '') IN ('', 'none')) AS tier_none "
    f"FROM {RECON_TBL} r LEFT JOIN {TIER_TBL} t ON t.flight_id = r.flight_id "
    "WHERE r.airline_name IS NOT NULL AND positionCaseInsensitive(r.airline_name, {q:String}) > 0 "
    "GROUP BY r.airline_name ORDER BY n_flights DESC, name LIMIT {limit:UInt64} OFFSET {offset:UInt64}"
)
AIRLINES_QUERY_NO_TIER = (
    "SELECT r.airline_name AS name, count() AS n_flights, uniqExact(r.callsign) AS n_services, "
    "toString(min(toDate(r.start_time, 'Asia/Tokyo'))) AS first_day, "
    "toString(max(toDate(r.start_time, 'Asia/Tokyo'))) AS last_day "
    f"FROM {RECON_TBL} r "
    "WHERE r.airline_name IS NOT NULL AND positionCaseInsensitive(r.airline_name, {q:String}) > 0 "
    "GROUP BY r.airline_name ORDER BY n_flights DESC, name LIMIT {limit:UInt64} OFFSET {offset:UInt64}"
)
AIRLINES_COUNT_QUERY = (
    f"SELECT uniqExact(airline_name) FROM {RECON_TBL} "
    "WHERE airline_name IS NOT NULL AND positionCaseInsensitive(airline_name, {q:String}) > 0"
)

_SERVICES_WHERE = (
    "r.callsign IS NOT NULL AND r.callsign != '' "
    "AND ({airline:String} = '' OR r.airline_name = {airline:String}) "
    "AND ({airline:String} != '' OR startsWith(upper(trimBoth(r.callsign)), {q:String}))"
)
SERVICES_QUERY_TIER = (
    "SELECT r.callsign AS callsign, count() AS n_instances, "
    "toString(min(toDate(r.start_time, 'Asia/Tokyo'))) AS first_day, "
    "toString(max(toDate(r.start_time, 'Asia/Tokyo'))) AS last_day, "
    "countIf(t.tier = 'settled') AS tier_settled, countIf(t.tier = 'estimated') AS tier_estimated, "
    "countIf(t.tier = 'provisional') AS tier_provisional, "
    "countIf(coalesce(t.tier, '') IN ('', 'none')) AS tier_none "
    f"FROM {RECON_TBL} r LEFT JOIN {TIER_TBL} t ON t.flight_id = r.flight_id "
    "WHERE " + _SERVICES_WHERE
    + " GROUP BY r.callsign ORDER BY n_instances DESC, callsign LIMIT {limit:UInt64} OFFSET {offset:UInt64}"
)
SERVICES_QUERY_NO_TIER = (
    "SELECT r.callsign AS callsign, count() AS n_instances, "
    "toString(min(toDate(r.start_time, 'Asia/Tokyo'))) AS first_day, "
    "toString(max(toDate(r.start_time, 'Asia/Tokyo'))) AS last_day "
    f"FROM {RECON_TBL} r WHERE " + _SERVICES_WHERE
    + " GROUP BY r.callsign ORDER BY n_instances DESC, callsign LIMIT {limit:UInt64} OFFSET {offset:UInt64}"
)
SERVICES_COUNT_QUERY = f"SELECT uniqExact(r.callsign) FROM {RECON_TBL} r WHERE " + _SERVICES_WHERE
# Top-3 O/D per callsign, restricted to the page's own callsigns — a plain (non-correlated) subquery,
# since ClickHouse has no correlated-subquery support to fold this into the main GROUP BY.
SERVICES_TOP_OD_QUERY = (
    "SELECT callsign, o, d, n FROM ("
    "SELECT r.callsign AS callsign, coalesce(r.origin_iata, r.origin_icao) AS o, "
    "coalesce(r.dest_iata, r.dest_icao) AS d, count() AS n, "
    "row_number() OVER (PARTITION BY r.callsign ORDER BY count() DESC, o, d) AS rn "
    f"FROM {RECON_TBL} r WHERE r.callsign IN {{callsigns:Array(String)}} "
    "AND coalesce(r.origin_iata, r.origin_icao) IS NOT NULL AND coalesce(r.dest_iata, r.dest_icao) IS NOT NULL "
    "GROUP BY r.callsign, o, d) WHERE rn <= 3 ORDER BY callsign, rn"
)

SEARCH_AIRLINES_QUERY = (
    f"SELECT airline_name AS name, count() AS n_flights FROM {RECON_TBL} "
    "WHERE airline_name IS NOT NULL AND positionCaseInsensitive(airline_name, {q:String}) > 0 "
    "GROUP BY airline_name ORDER BY n_flights DESC, name LIMIT {limit:UInt64}"
)
SEARCH_SERVICES_QUERY = (
    f"SELECT callsign, any(airline_name) AS airline, count() AS n_instances FROM {RECON_TBL} "
    "WHERE callsign IS NOT NULL AND callsign != '' AND startsWith(upper(trimBoth(callsign)), {svc_q:String}) "
    "GROUP BY callsign ORDER BY n_instances DESC, callsign LIMIT {limit:UInt64}"
)
SEARCH_AIRFRAMES_QUERY = (
    # aggregate aliases must not shadow the WHERE-referenced source columns (registration) — CH
    # resolves the bare name to the SELECT alias and rejects the aggregate in WHERE otherwise
    f"SELECT lower(icao24) AS icao24_out, any(registration) AS reg_out, any(typecode) AS type_out, "
    f"count() AS n_instances FROM {RECON_TBL} "
    "WHERE icao24 IS NOT NULL AND (startsWith(lower(icao24), {hex_q:String}) "
    "OR startsWith(upper(replaceAll(coalesce(registration, ''), '-', '')), {reg_q:String})) "
    "GROUP BY icao24 ORDER BY n_instances DESC, icao24_out LIMIT {limit:UInt64}"
)
SEARCH_AIRPORTS_QUERY = (
    "SELECT icao, iata, name, city FROM silver_ch.dim_airports "
    "WHERE startsWith(upper(icao), {code_q:String}) OR startsWith(upper(coalesce(iata, '')), {code_q:String}) "
    "OR positionCaseInsensitive(name, {q:String}) > 0 OR positionCaseInsensitive(city, {q:String}) > 0 "
    "ORDER BY name, icao LIMIT {limit:UInt64}"
)


def clamp(limit, cap) -> int:
    # Floor 0 so a negative/garbage query value can't request a negative-sized page.
    return max(0, min(limit, cap))


def parse_day(s) -> "datetime.date | None":
    # Blank/malformed query text means "unset" — instances_params supplies the wide sentinel range.
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s.strip())
    except ValueError:
        return None


def _utc(dt):
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp() if dt is not None else None


def parse_od(od: str):
    # "HND-ITM" -> ("HND", "ITM"); anything else (blank, no dash) disables the filter on both sides.
    parts = (od or "").strip().upper().split("-")
    return (parts[0], parts[1]) if len(parts) == 2 and parts[0] and parts[1] else ("", "")


def instances_params(callsign, airline, hex_, reg, airport, od, type_, military, day_from, day_to) -> dict:
    od_o, od_d = parse_od(od)
    return {
        "callsign": (callsign or "").strip().upper(),
        "airline": (airline or "").strip(),
        "hex": (hex_ or "").strip().lower(),
        "reg": (reg or "").strip().upper(),
        "airport": (airport or "").strip().upper(),
        "od_o": od_o, "od_d": od_d,
        "type": (type_ or "").strip(),
        "military": 1 if military else 0,
        "day_from": day_from or datetime.date(1900, 1, 1),
        "day_to": day_to or datetime.date(2999, 12, 31),
    }


def shape_instance_row(row) -> dict:
    (flight_id, day, start_time, end_time, icao24, registration, typecode, callsign, airline,
     o_icao, o_iata, o_city, d_icao, d_iata, d_city, tier, gap_s, n_points, is_mil) = row
    return {
        "flight_id": flight_id, "day": day,
        "start_ts": _utc(start_time), "end_ts": _utc(end_time),
        "icao24": icao24, "registration": registration, "typecode": typecode,
        "callsign": (callsign or "").strip() or None, "airline": airline,
        "origin": {"icao": o_icao, "iata": o_iata, "city": o_city},
        "dest": {"icao": d_icao, "iata": d_iata, "city": d_city},
        "tier": tier, "effective_gap_s": gap_s, "n_points": n_points,
        "is_military": bool(is_mil),
    }


def shape_od_breakdown(rows) -> list:
    return [{"o": o, "d": d, "n": n} for o, d, n in rows]


def shape_airline_row(row, with_tier: bool) -> dict:
    name, n_flights, n_services, first_day, last_day, *tier_counts = row
    if with_tier:
        settled, estimated, provisional, none_ = tier_counts
        tiers = {"settled": settled, "estimated": estimated, "provisional": provisional, "none": none_}
    else:
        tiers = {}
    return {"name": name, "n_flights": n_flights, "n_services": n_services,
            "first_day": first_day, "last_day": last_day, "tiers": tiers}


def group_top_od(rows) -> dict:
    # rows already arrive rn<=3 pre-filtered and ordered — group is a pure reshape, no re-ranking here.
    out: dict = {}
    for callsign, o, d, n in rows:
        out.setdefault(callsign, []).append({"o": o, "d": d, "n": n})
    return out


def search_params(q: str) -> dict:
    stripped = (q or "").strip()
    return {
        "q": stripped,
        "svc_q": stripped.upper(),
        "hex_q": stripped.lower(),
        "reg_q": stripped.upper().replace("-", ""),
        "code_q": stripped.upper(),
    }


def shape_search_airline(row) -> dict:
    name, n_flights = row
    return {"name": name, "n_flights": n_flights}


def shape_search_service(row) -> dict:
    callsign, airline, n_instances = row
    return {"callsign": callsign, "airline": airline, "n_instances": n_instances}


def shape_search_airframe(row) -> dict:
    icao24, registration, typecode, n_instances = row
    return {"icao24": icao24, "registration": registration, "typecode": typecode, "n_instances": n_instances}


def shape_search_airport(row) -> dict:
    icao, iata, name, city = row
    return {"icao": icao, "iata": iata, "name": name, "city": city}


def shape_service_row(row, with_tier: bool, top_od_by_callsign: dict) -> dict:
    callsign, n_instances, first_day, last_day, *tier_counts = row
    if with_tier:
        settled, estimated, provisional, none_ = tier_counts
        tiers = {"settled": settled, "estimated": estimated, "provisional": provisional, "none": none_}
    else:
        tiers = {}
    return {"callsign": callsign, "n_instances": n_instances,
            "top_od": top_od_by_callsign.get(callsign, []),
            "first_day": first_day, "last_day": last_day, "tiers": tiers}
