import datetime
import os

# Config-free like pathfusion.py — the schema knob is read once here, not threaded from app.py.
CH_DB = os.environ.get("LIVEMAP_CH_DB", "gold_ch")
TIER_TBL = f"{CH_DB}.fct_flight_recon_tier"
RECON_TBL = f"{CH_DB}.fct_flights_reconciled"
FLAGS_TBL = f"{CH_DB}.fct_flight_flags"
EST_TBL = f"{CH_DB}.fct_est_settlement"
EST_BREAKDOWN_TBL = f"{CH_DB}.agg_est_breakdown_daily"

# The module's declared set of deploy-order-optional marts — every mart a fetcher degrades on.
OPTIONAL_TABLES = ("fct_flight_flags", "fct_flight_recon_tier", "fct_est_settlement",
                   "agg_est_breakdown_daily")
# Only ever run on a degradation path: the happy path pays one round trip and lets an
# unknown-table error tell it which optional mart is missing.
PROBE_TABLES_QUERY = (
    "SELECT name FROM system.tables WHERE database = {db:String} "
    "AND name IN (" + ", ".join(f"'{t}'" for t in OPTIONAL_TABLES) + ")"
)

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

_OD_BREAKDOWN_SELECT = (
    "SELECT coalesce(r.origin_iata, r.origin_icao) AS o, coalesce(r.dest_iata, r.dest_icao) AS d, count() AS n "
)
_OD_BREAKDOWN_TAIL = (
    "AND coalesce(r.origin_iata, r.origin_icao) IS NOT NULL AND coalesce(r.dest_iata, r.dest_icao) IS NOT NULL "
    "GROUP BY o, d ORDER BY n DESC, o, d LIMIT 8"
)


# Fixed-catalog builders: the booleans only ever select among the module's own fragments above,
# so no caller-provided text can reach the SQL (the military clause rides the tier variant only).
def instances_query(tier: bool, asc: bool) -> str:
    return (_INSTANCES_SELECT + (_INSTANCES_TIER_COLS if tier else _INSTANCES_NO_TIER_COLS)
            + (_TIER_JOIN if tier else _NO_TIER_FROM) + _INSTANCES_WHERE
            + (_MIL_CLAUSE if tier else "") + (_PAGE_ASC if asc else _PAGE_DESC))


def instances_count_query(tier: bool) -> str:
    return ("SELECT count() " + (_TIER_JOIN if tier else _NO_TIER_FROM) + _INSTANCES_WHERE
            + (_MIL_CLAUSE if tier else ""))


def instances_od_breakdown_query(tier: bool) -> str:
    return (_OD_BREAKDOWN_SELECT + (_TIER_JOIN if tier else _NO_TIER_FROM) + _INSTANCES_WHERE_NO_OD
            + (_MIL_CLAUSE if tier else "") + " " + _OD_BREAKDOWN_TAIL)

# fct_flight_flags.start_day is the UTC day — the feed windows on the reconciled start_time in JST
# so a 23:30 UTC flight lands on the same day the rest of the workbench puts it.
_FLAGS_WHERE = (
    "({class:String} = '' OR f.flag_class = {class:String}) "
    "AND toDate(r.start_time, 'Asia/Tokyo') BETWEEN {day_from:Date} AND {day_to:Date}"
)
_FLAGS_PAGE = (" ORDER BY r.start_time DESC, r.flight_id DESC, f.flag_class ASC"
               " LIMIT {limit:UInt64} OFFSET {offset:UInt64}")


def _flags_query(tier: bool) -> str:
    return (_INSTANCES_SELECT + (_INSTANCES_TIER_COLS if tier else _INSTANCES_NO_TIER_COLS)
            + ", f.flag_class AS flag_class, f.detail AS detail "
            f"FROM {FLAGS_TBL} f JOIN {RECON_TBL} r ON r.flight_id = f.flight_id "
            + (f"LEFT JOIN {TIER_TBL} t ON t.flight_id = r.flight_id " if tier else "")
            + "WHERE " + _FLAGS_WHERE + _FLAGS_PAGE)


FLAGS_QUERY_TIER = _flags_query(True)
FLAGS_QUERY_NO_TIER = _flags_query(False)
FLAGS_COUNT_QUERY = (
    f"SELECT count() FROM {FLAGS_TBL} f JOIN {RECON_TBL} r ON r.flight_id = f.flight_id WHERE "
    + _FLAGS_WHERE
)
FLAGS_CLASSES_QUERY = (
    f"SELECT f.flag_class, count() FROM {FLAGS_TBL} f JOIN {RECON_TBL} r "
    "ON r.flight_id = f.flight_id "
    "WHERE toDate(r.start_time, 'Asia/Tokyo') BETWEEN {day_from:Date} AND {day_to:Date} "
    "GROUP BY f.flag_class ORDER BY f.flag_class"
)

# One round trip for the whole overview: every section is a UNION ALL arm over the same window CTE,
# projecting the one (sect, k, k2, v1, v2) shape. Optional-mart arms drop out when the mart is gone.
_SUMMARY_RECON_CTE = (
    "WITH wr AS (SELECT *, toDate(start_time, 'Asia/Tokyo') AS jday "
    f"FROM {RECON_TBL} "
    "WHERE toDate(start_time, 'Asia/Tokyo') BETWEEN {day_from:Date} AND {day_to:Date}) "
)
SUMMARY_CORE_ARMS = (
    "SELECT 'total' AS sect, 'flights' AS k, '' AS k2, toFloat64(count()) AS v1, 0. AS v2 FROM wr "
    "UNION ALL SELECT 'total', 'aircraft', '', toFloat64(uniqExact(icao24)), 0. FROM wr "
    "UNION ALL SELECT 'total', 'services', '', toFloat64(uniqExact(callsign)), 0. FROM wr "
    "WHERE coalesce(callsign, '') != '' "
    "UNION ALL SELECT 'daily', toString(jday), '', toFloat64(count()), 0. FROM wr GROUP BY jday "
    "UNION ALL SELECT 'mover', k, '', v1, v2 FROM ("
    "SELECT c.k AS k, toFloat64(c.n) AS v1, toFloat64(coalesce(p.n, 0)) AS v2 "
    "FROM (SELECT concat(coalesce(origin_iata, origin_icao), '-', coalesce(dest_iata, dest_icao)) AS k, "
    "count() AS n "
    "FROM wr WHERE origin_icao IS NOT NULL AND dest_icao IS NOT NULL GROUP BY k) c "
    "LEFT JOIN (SELECT concat(coalesce(origin_iata, origin_icao), '-', coalesce(dest_iata, dest_icao)) AS k, "
    "count() AS n "
    f"FROM {RECON_TBL} "
    "WHERE toDate(start_time, 'Asia/Tokyo') BETWEEN {prev_from:Date} AND {prev_to:Date} "
    "AND origin_icao IS NOT NULL AND dest_icao IS NOT NULL GROUP BY k) p ON p.k = c.k "
    "ORDER BY v1 DESC, k LIMIT 5)"
)
SUMMARY_FLAGS_ARMS = (
    " UNION ALL SELECT 'flag', flag_class, '', toFloat64(count()), 0. "
    f"FROM {FLAGS_TBL} f JOIN wr r ON r.flight_id = f.flight_id GROUP BY flag_class "
    "UNION ALL SELECT 'flagged', '', '', toFloat64(uniqExact(f.flight_id)), 0. "
    f"FROM {FLAGS_TBL} f JOIN wr r ON r.flight_id = f.flight_id"
)
SUMMARY_TIER_ARMS = (
    " UNION ALL SELECT 'tier', if(coalesce(t.tier, '') = '', 'unknown', t.tier), '', toFloat64(count()), 0. "
    f"FROM wr r LEFT JOIN {TIER_TBL} t ON t.flight_id = r.flight_id GROUP BY 2 "
    "UNION ALL SELECT 'tier_daily', toString(r.jday), if(coalesce(t.tier, '') = '', 'unknown', t.tier), "
    "toFloat64(count()), 0. "
    f"FROM wr r LEFT JOIN {TIER_TBL} t ON t.flight_id = r.flight_id GROUP BY 2, 3"
)
# computed_at is the estimate's own serve day — the only day fct_est_settlement carries, so the
# est arms window on it rather than on the flight's JST start day.
_EST_DEDUP = (
    f"SELECT * FROM {EST_TBL} "
    "WHERE skip_ambiguous = 0 AND settled = 1 AND err_p50_km IS NOT NULL "
    "AND toDate(computed_at, 'Asia/Tokyo') BETWEEN {day_from:Date} AND {day_to:Date} "
    "ORDER BY computed_at, estimate_id LIMIT 1 BY input_fingerprint, seg_idx"
)
# The standing drift read (README): unique scored inputs, per-point errors pooled — full-recompute
# rows repeat an input, and a median of per-segment medians measurably hides drift.
_EST_POOL_ARR = "arraySort(groupArrayArray(errs_km)) AS pool"


def _pool_pct(p: float, alias: str) -> str:
    # One ceil-indexing convention for every percentile read; -1 is the empty-pool sentinel.
    return f"if(length(pool) = 0, -1., toFloat64(pool[toUInt32(ceil({p} * length(pool)))])) AS {alias}"


_EST_POOL = f"{_EST_POOL_ARR}, " + _pool_pct(0.5, "v1") + ", toFloat64(count()) AS v2"
SUMMARY_EST_ARMS = (
    " UNION ALL SELECT 'est', 'p50', '', v1, v2 FROM ("
    f"SELECT {_EST_POOL} FROM ({_EST_DEDUP})) "
    "UNION ALL SELECT 'est_daily', k, '', v1, v2 FROM ("
    "SELECT toString(toDate(computed_at, 'Asia/Tokyo')) AS k, "
    f"{_EST_POOL} FROM ({_EST_DEDUP}) GROUP BY k)"
)


# Estimates view: the same deduped pool as the summary tile, split per config_hash so an
# instrument change reads as a break in the series rather than blending into the one before it.
_EST_POOL_PCTS = f"{_EST_POOL_ARR}, " + _pool_pct(0.5, "p50") + ", " + _pool_pct(0.9, "p90")
# config_hash is UInt64 — it goes out as a string, since JS Numbers lose the low bits.
# The `cfg` alias is deliberate: an alias named for its own source column shadows it in GROUP BY.
ESTIMATES_HEADLINE_QUERY = (
    "SELECT cfg, n, p50, p90, first_day, last_day FROM ("
    "SELECT toString(config_hash) AS cfg, toUInt64(count()) AS n, "
    f"{_EST_POOL_PCTS}, "
    "toString(min(toDate(computed_at, 'Asia/Tokyo'))) AS first_day, "
    "toString(max(toDate(computed_at, 'Asia/Tokyo'))) AS last_day, "
    "max(computed_at) AS last_seen "
    f"FROM ({_EST_DEDUP}) GROUP BY config_hash) ORDER BY last_seen DESC, cfg"
)
ESTIMATES_DAILY_QUERY = (
    "SELECT day, cfg, p50, p90, n FROM ("
    "SELECT toString(toDate(computed_at, 'Asia/Tokyo')) AS day, toString(config_hash) AS cfg, "
    f"{_EST_POOL_PCTS}, toUInt64(count()) AS n "
    f"FROM ({_EST_DEDUP}) GROUP BY day, config_hash) ORDER BY day, cfg"
)
# The breakdown mart is UTC-day grain (toDate(computed_at)) while the series above is JST — a
# deliberate seam: a day-grain mart carries no sub-day detail to re-bucket honestly.
ESTIMATES_MIX_QUERY = (
    "SELECT dimension, value, producer, toUInt64(sum(n)) AS n "
    f"FROM {EST_BREAKDOWN_TBL} WHERE day BETWEEN {{day_from:Date}} AND {{day_to:Date}} "
    "GROUP BY dimension, value, producer ORDER BY dimension, n DESC, value, producer"
)
# Raw rows, NOT the deduped pool: these count the logging stream itself (what got settled, what is
# still awaiting truth, what was dropped as ambiguous), which dedup would understate.
# Aliases must not reuse a source column name — `AS settled` makes the next countIf nest aggregates.
ESTIMATES_OUTCOMES_QUERY = (
    "SELECT toUInt64(countIf(settled = 1)) AS n_settled, "
    "toUInt64(countIf(settled = 0 AND skip_ambiguous = 0)) AS n_awaiting, "
    "toUInt64(countIf(skip_ambiguous = 1)) AS n_ambiguous, "
    "toUInt64(countIf(input_provisional = 1)) AS n_input_provisional, "
    "toUInt64(countIf(input_provisional = 0)) AS n_input_settled "
    f"FROM {EST_TBL} "
    "WHERE toDate(computed_at, 'Asia/Tokyo') BETWEEN {day_from:Date} AND {day_to:Date}"
)
MIX_DIMENSIONS = ("skip", "segment_kind", "uncertainty_bin")

# Coverage windows on the reconciled JST start day (the flags pattern) — the tier mart's own
# start_day is UTC, so windowing on it would split a 23:30 UTC flight off from its JST day.
_COVERAGE_JOIN = (
    f"FROM {RECON_TBL} r LEFT JOIN {TIER_TBL} t ON t.flight_id = r.flight_id "
    "WHERE toDate(r.start_time, 'Asia/Tokyo') BETWEEN {day_from:Date} AND {day_to:Date}"
)
COVERAGE_TIER_DAILY_QUERY = (
    "SELECT toString(toDate(r.start_time, 'Asia/Tokyo')) AS day, "
    "if(coalesce(t.tier, '') = '', 'unknown', t.tier) AS tier, toUInt64(count()) AS n "
    + _COVERAGE_JOIN + " GROUP BY day, tier ORDER BY day, tier"
)
# Fixed edges (issue #171 ruling 3); 900 s is the tier seam and has to be an exact edge. roundDown
# maps a gap to its bin's lower bound, so the edges stay in the SQL rather than in the shaper.
GAP_EDGES = (0, 60, 300, 900, 3600, 10800, 21600, 43200)
COVERAGE_GAP_HIST_QUERY = (
    f"SELECT toUInt64(roundDown(t.largest_gap_s, {list(GAP_EDGES)})) AS ge, toUInt64(count()) AS n "
    + _COVERAGE_JOIN + " AND t.largest_gap_s IS NOT NULL GROUP BY ge ORDER BY ge"
)
# observed_fraction is a per-flight value, so the per-day median IS the pooled read — never a
# mean of aggregates.
COVERAGE_OBSERVED_QUERY = (
    "SELECT toString(toDate(r.start_time, 'Asia/Tokyo')) AS day, "
    "toFloat64(quantileExact(0.5)(t.observed_fraction)) AS med, toUInt64(count()) AS n "
    + _COVERAGE_JOIN + " AND t.observed_fraction IS NOT NULL GROUP BY day ORDER BY day"
)


def summary_query(has_flags: bool, has_tier: bool, has_est: bool) -> str:
    # core first so the union's column names come from a section that always exists
    return (_SUMMARY_RECON_CTE + SUMMARY_CORE_ARMS
            + (SUMMARY_FLAGS_ARMS if has_flags else "")
            + (SUMMARY_TIER_ARMS if has_tier else "")
            + (SUMMARY_EST_ARMS if has_est else ""))


# The wire `dim` never reaches SQL: it only selects among these three pre-built query texts.
_TRENDS_K = {
    "route": "concat(coalesce(origin_iata, origin_icao), '-', coalesce(dest_iata, dest_icao))",
    "airline": "airline_name",
    "airport": ("arrayJoin(arrayDistinct(arrayFilter(x -> x != '', "
                "[coalesce(origin_iata, origin_icao, ''), coalesce(dest_iata, dest_icao, '')])))"),
}
# arrayDistinct above makes an o == d flight count once for that airport, not twice.
_TRENDS_GUARD = {
    "route": " AND origin_icao IS NOT NULL AND dest_icao IS NOT NULL",
    "airline": " AND airline_name IS NOT NULL",
    "airport": "",
}
_TRENDS_CUR_WINDOW = "WHERE toDate(start_time, 'Asia/Tokyo') BETWEEN {day_from:Date} AND {day_to:Date}"
_TRENDS_PREV_WINDOW = "WHERE toDate(start_time, 'Asia/Tokyo') BETWEEN {prev_from:Date} AND {prev_to:Date}"


def _trends_inner(dim: str, window: str, with_ac: bool) -> str:
    ac = ", uniqExact(icao24) AS ac" if with_ac else ""
    return (f"SELECT {_TRENDS_K[dim]} AS k, count() AS n{ac} FROM {RECON_TBL} "
            + window + _TRENDS_GUARD[dim] + " GROUP BY k")


TRENDS_RANK_QUERY = {
    dim: ("SELECT c.k AS k, c.n AS n, c.ac AS distinct_aircraft, toInt64(coalesce(p.n, 0)) AS prev_n "
          f"FROM ({_trends_inner(dim, _TRENDS_CUR_WINDOW, True)}) c "
          f"LEFT JOIN ({_trends_inner(dim, _TRENDS_PREV_WINDOW, False)}) p ON p.k = c.k "
          "ORDER BY n DESC, k LIMIT {limit:UInt64} OFFSET {offset:UInt64}")
    for dim in _TRENDS_K
}
TRENDS_SERIES_QUERY = {
    dim: (f"SELECT {_TRENDS_K[dim]} AS k, toString(toDate(start_time, 'Asia/Tokyo')) AS day, "
          f"count() AS n FROM {RECON_TBL} " + _TRENDS_CUR_WINDOW + _TRENDS_GUARD[dim]
          + " AND k IN {keys:Array(String)} GROUP BY k, day ORDER BY k, day")
    for dim in _TRENDS_K
}
TRENDS_TOTAL_QUERY = {
    dim: f"SELECT uniqExact(k) FROM ({_trends_inner(dim, _TRENDS_CUR_WINDOW, False)})"
    for dim in _TRENDS_K
}

# Shared by every tier-mix aggregate (airlines/services); the no-tier fallback simply omits it.
_TIER_AGG_COLS = (
    "countIf(t.tier = 'settled') AS tier_settled, countIf(t.tier = 'estimated') AS tier_estimated, "
    "countIf(t.tier = 'provisional') AS tier_provisional, "
    "countIf(coalesce(t.tier, '') IN ('', 'none')) AS tier_none "
)


def _tier_pair(select_head: str, where_tail: str) -> "tuple[str, str]":
    # Each aggregate's SQL is written once: the tier variant appends the shared tier-mix columns
    # plus the LEFT JOIN, the fallback keeps the head verbatim over the bare reconciled mart.
    return (select_head + ", " + _TIER_AGG_COLS + _TIER_JOIN + where_tail,
            select_head + " " + _NO_TIER_FROM + where_tail)


AIRLINES_QUERY_TIER, AIRLINES_QUERY_NO_TIER = _tier_pair(
    "SELECT r.airline_name AS name, count() AS n_flights, uniqExact(r.callsign) AS n_services, "
    "toString(min(toDate(r.start_time, 'Asia/Tokyo'))) AS first_day, "
    "toString(max(toDate(r.start_time, 'Asia/Tokyo'))) AS last_day",
    "r.airline_name IS NOT NULL AND positionCaseInsensitive(r.airline_name, {q:String}) > 0 "
    "GROUP BY r.airline_name ORDER BY n_flights DESC, name LIMIT {limit:UInt64} OFFSET {offset:UInt64}",
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
SERVICES_QUERY_TIER, SERVICES_QUERY_NO_TIER = _tier_pair(
    "SELECT r.callsign AS callsign, count() AS n_instances, "
    "toString(min(toDate(r.start_time, 'Asia/Tokyo'))) AS first_day, "
    "toString(max(toDate(r.start_time, 'Asia/Tokyo'))) AS last_day",
    _SERVICES_WHERE
    + " GROUP BY r.callsign ORDER BY n_instances DESC, callsign LIMIT {limit:UInt64} OFFSET {offset:UInt64}",
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


def _day_params(day_from, day_to) -> dict:
    # Blank/malformed days bind a wide sentinel range rather than dropping the clause.
    return {"day_from": day_from or datetime.date(1900, 1, 1),
            "day_to": day_to or datetime.date(2999, 12, 31)}


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
    } | _day_params(day_from, day_to)


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


def flags_params(class_, day_from, day_to) -> dict:
    # An unrecognised class just binds and matches nothing — no server-side allow-list to drift.
    return {"class": (class_ or "").strip()} | _day_params(day_from, day_to)


def estimates_params(day_from, day_to) -> dict:
    return _day_params(day_from, day_to)


def coverage_params(day_from, day_to) -> dict:
    return _day_params(day_from, day_to)


def shape_flag_row(row) -> dict:
    return shape_instance_row(row[:19]) | {"flag_class": row[19], "detail": row[20]}


def _delta_pct(n, prev_n):
    # An absent previous window (or a brand-new key) has no baseline — null, never a fake 100%.
    return round((n - prev_n) / prev_n * 100, 1) if prev_n > 0 else None


def _prev_window(day_from, day_to):
    # Subtracting past the sentinel floor raises OverflowError, so bound the step by the distance to it.
    span = (day_to - day_from).days + 1
    back = max(0, (day_from - datetime.date(1900, 1, 1)).days)
    return (day_from - datetime.timedelta(days=min(max(span, 0), back)),
            day_from - datetime.timedelta(days=min(1, back)))


def _window_params(day_from, day_to) -> dict:
    if day_from is not None and day_to is not None:
        prev_from, prev_to = _prev_window(day_from, day_to)
    else:
        # No explicit range means no comparable previous window — an empty one zeroes every prev_n.
        prev_from = prev_to = datetime.date(1900, 1, 1)
    return {
        "day_from": day_from or datetime.date(1900, 1, 1),
        "day_to": day_to or datetime.date(2999, 12, 31),
        "prev_from": prev_from, "prev_to": prev_to,
    }


def summary_params(day_from, day_to) -> dict:
    return _window_params(day_from, day_to)


def trends_params(day_from, day_to, limit, offset) -> dict:
    return _window_params(day_from, day_to) | {"limit": limit, "offset": offset}


def empty_summary() -> dict:
    return {"flights": 0, "aircraft": 0, "services": 0, "daily": [],
            "flags": {"available": False, "flagged": 0, "classes": {}},
            "tiers": {"available": False, "mix": {}, "daily": []},
            "est": {"available": False, "err_p50_km": None, "n": 0, "daily": []},
            "movers": []}


def empty_trends(dim: str, limit, offset) -> dict:
    return {"dim": dim, "grain": "day", "series": [], "rank": [], "total": 0,
            "limit": limit, "offset": offset}


def shape_summary(rows, has_flags: bool, has_tier: bool, has_est: bool) -> dict:
    out = empty_summary()
    daily: dict = {}
    tier_daily: dict = {}
    est_daily: dict = {}
    for sect, k, k2, v1, v2 in rows:
        if sect == "total" and k in ("flights", "aircraft", "services"):
            out[k] = int(v1)
        elif sect == "daily":
            daily[k] = int(v1)
        elif sect == "mover":
            out["movers"].append({"key": k, "n": int(v1), "prev_n": int(v2),
                                  "delta_pct": _delta_pct(int(v1), int(v2))})
        elif sect == "flag":
            out["flags"]["classes"][k] = int(v1)
        elif sect == "flagged":
            out["flags"]["flagged"] = int(v1)
        elif sect == "tier":
            out["tiers"]["mix"][k] = int(v1)
        elif sect == "tier_daily":
            if int(v1):
                tier_daily.setdefault(k, {})[k2] = int(v1)
        elif sect == "est":
            out["est"]["n"] = int(v2)
            out["est"]["err_p50_km"] = round(v1, 3) if int(v2) > 0 else None
        elif sect == "est_daily":
            est_daily[k] = [round(v1, 3), int(v2)]
    # UNION ALL block interleaving may not preserve the mover arm's inner ORDER BY — re-impose it
    out["movers"].sort(key=lambda m: (-m["n"], m["key"]))
    out["daily"] = [[d, daily[d]] for d in sorted(daily)]
    out["flags"]["available"] = has_flags
    out["tiers"]["available"] = has_tier
    out["tiers"]["daily"] = [[d, tier_daily[d]] for d in sorted(tier_daily)]
    out["est"]["available"] = has_est
    out["est"]["daily"] = [[d] + est_daily[d] for d in sorted(est_daily)]
    return out


def empty_estimates() -> dict:
    return {"available": True, "headline": [], "daily": [],
            "mix": {"available": True} | {d: [] for d in MIX_DIMENSIONS},
            "outcomes": {"settled": 0, "awaiting": 0, "ambiguous": 0},
            "input_split": {"provisional": 0, "settled": 0}}


def empty_coverage() -> dict:
    return {"available": True, "tier_daily": [], "gap_bins": [], "observed": []}


def _pooled_km(v):
    # -1 is the empty-pool sentinel from the pooled quantile expression, never a real distance
    return round(v, 3) if v is not None and v >= 0 else None


def shape_estimates(headline_rows, daily_rows, mix_rows, outcomes_row, mix_available: bool) -> dict:
    out = empty_estimates()
    out["headline"] = [
        {"config_hash": cfg, "n": int(n), "p50_km": _pooled_km(p50), "p90_km": _pooled_km(p90),
         "first_day": first_day, "last_day": last_day}
        for cfg, n, p50, p90, first_day, last_day in headline_rows
    ]
    out["daily"] = [
        {"day": day, "config_hash": cfg, "p50_km": _pooled_km(p50), "p90_km": _pooled_km(p90),
         "n": int(n)}
        for day, cfg, p50, p90, n in daily_rows
    ]
    out["mix"]["available"] = mix_available
    for dim, value, producer, n in mix_rows:
        # a dimension the view has no panel for would render nowhere — drop it rather than carry it
        if dim in MIX_DIMENSIONS:
            out["mix"][dim].append({"value": value, "producer": producer, "n": int(n)})
    if outcomes_row:
        settled, awaiting, ambiguous, prov_in, settled_in = outcomes_row
        out["outcomes"] = {"settled": int(settled), "awaiting": int(awaiting),
                           "ambiguous": int(ambiguous)}
        out["input_split"] = {"provisional": int(prov_in), "settled": int(settled_in)}
    return out


def shape_coverage(tier_rows, gap_rows, obs_rows) -> dict:
    tier_daily: dict = {}
    for day, tier, n in tier_rows:
        if int(n):
            tier_daily.setdefault(day, {})[tier] = int(n)
    by_edge = {int(ge): int(n) for ge, n in gap_rows}
    # every bin is always present (0 where empty), so the histogram's shape can't shift under the eye
    bins = [{"ge": lo, "lt": GAP_EDGES[i + 1] if i + 1 < len(GAP_EDGES) else None,
             "n": by_edge.get(lo, 0)}
            for i, lo in enumerate(GAP_EDGES)]
    return {"available": True,
            "tier_daily": [[d, tier_daily[d]] for d in sorted(tier_daily)],
            "gap_bins": bins,
            "observed": [{"day": day, "median": round(med, 4), "n": int(n)}
                         for day, med, n in obs_rows]}


def shape_trends(dim: str, rank_rows, series_rows, total, limit, offset) -> dict:
    rank = [{"key": k, "n": int(n), "distinct_aircraft": int(ac), "prev_n": int(prev_n),
             "delta_pct": _delta_pct(int(n), int(prev_n))}
            for k, n, ac, prev_n in rank_rows]
    points: dict = {}
    for k, day, n in series_rows:
        points.setdefault(k, []).append([day, int(n)])
    return {"dim": dim, "grain": "day",
            "series": [{"key": r["key"], "points": points.get(r["key"], [])} for r in rank],
            "rank": rank, "total": int(total), "limit": limit, "offset": offset}


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
