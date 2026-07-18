from __future__ import annotations

import logging
import os
import time
from typing import Callable

log = logging.getLogger(__name__)

# Served CH marts must trail wall-clock by <= this or a stalled dbt CH lane serves stale dashboards;
# 2h is generous (hour-bucket grain + ingest cadence) so it only fires on a real stall. Covers the
# states/context + rooftop ADS-B lanes (both sub-hourly / continuous).
_FRESHNESS_LAG_TOL_S = 7200
# The flights lane is a DAILY ingest (with a 48h arrival lag), so its marts get a much larger window —
# a 3-day-stale fact_flights means the daily transform_flights lane has stopped, not normal cadence.
_FLIGHTS_FRESHNESS_LAG_TOL_S = 259200
# Bound a stalled backend so this gate can't block the worker indefinitely on client defaults.
_TIMEOUT_S = 60
# The source-of-truth gate reads the Garage Parquet (ground truth) via this named collection in s3().
_GARAGE_COLLECTION = "garage"
# Closed-window seconds: data older than (now - this) is guaranteed loaded (tableize lag is minutes << 2h), so
# the in-flight ingest trail is excluded on BOTH sides and CH must match the source EXACTLY (no eps — a relative
# tolerance hides a missing object: a states file is ~hundreds of rows, far under any % of a 23M table).
_CLOSED_WINDOW_S = 7200
# opensky_states distinct-CONTENT fingerprint (the same 19 source cols as the bronze _dedup_fp, committed_at
# excluded). Compared as uniqExact, this collapses the ReplacingMergeTree replay surplus (replays share the fp)
# while keeping every legit same-grain recapture VISIBLE (distinct fp) — a bare (icao24,snapshot_time) grain
# would hide a lost recapture (23.04M distinct content vs 22.67M grains). Computed inline (not via the _dedup_fp
# column) so the gate works on the table both before and after the RMT migration.
_STATES_CONTENT_FP = (
    "cityHash64(toString(tuple(icao24, callsign, origin_country, time_position, last_contact, longitude, "
    "latitude, baro_altitude, on_ground, velocity, true_track, vertical_rate, geo_altitude, squawk, spi, "
    "position_source, snapshot_time, region, ingested_at)))"
)

# Pin only the columns each check reads (CH reads Parquet columns by name) so an empty/fresh-deploy glob returns
# 0 rows instead of erroring 636 (CANNOT_EXTRACT_TABLE_STRUCTURE) — the gate runs */15 from first boot.
_STATES_SRC_STRUCT = (
    "icao24 Nullable(String), callsign Nullable(String), origin_country Nullable(String), "
    "time_position Nullable(Int64), last_contact Nullable(Int64), longitude Nullable(Float64), "
    "latitude Nullable(Float64), baro_altitude Nullable(Float64), on_ground Nullable(Bool), "
    "velocity Nullable(Float64), true_track Nullable(Float64), vertical_rate Nullable(Float64), "
    "geo_altitude Nullable(Float64), squawk Nullable(String), spi Nullable(Bool), "
    "position_source Nullable(Int64), snapshot_time Nullable(Int32), region Nullable(String), "
    "ingested_at Nullable(String)"
)
_ADSB_SRC_STRUCT = "capture_ts Nullable(Float64), hex Nullable(String)"
_FLIGHTS_SRC_STRUCT = "ingested_at Nullable(String)"
_ADSBLOL_SRC_STRUCT = "icao24 Nullable(String)"  # count()-only; one real column is enough to pin the schema

# Broken-on-start tripwire (#116): a hard crash with fsync off silently detaches a 0-byte part on restart,
# and the four accumulate-forever _acc marts have no rederivation path (an operator reseed is the only fix) —
# one sat invisible for two days until the value gate happened to notice. __dbt_ tables are ephemeral dbt
# temp/backup relations that rebuild every tick; 'system' is CH's own housekeeping logs, neither is warehouse data.
_BROKEN_PARTS_SQL = (
    "SELECT concat(database, '.', table, '/', name) FROM system.detached_parts "
    "WHERE reason = 'broken-on-start' AND database != 'system' AND table NOT ILIKE '%\\_\\_dbt\\_%'"
)


def _closed_cutoff() -> int:
    # One hour-aligned cutoff (UTC epoch, 2h ago) captured ONCE per gate run and substituted as a literal into
    # BOTH the CH and source SQL of every windowed check — so the two queries can never straddle an hour boundary
    # and disagree on the window (an exact gate would false-red on that race).
    return int(time.time()) // 3600 * 3600 - _CLOSED_WINDOW_S


def _ch_query(sql: str):
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        connect_timeout=10,
        send_receive_timeout=_TIMEOUT_S,
        # Match the dbt CH lane's NULL-on-no-match join semantics for any join in a spot-check.
        settings={"join_use_nulls": 1},
    )
    try:
        return client.query(sql).result_rows
    finally:
        client.close()


def _ch_query_serving(sql: str):
    # The serving gate connects as the SAME read-only user Superset uses (not the default admin), so it also
    # catches superset_ro password/permission drift — a green default-user check wouldn't reflect a broken dashboard.
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CH_SUPERSET_USER", "superset_ro"),
        password=os.environ.get("CH_SUPERSET_PASSWORD", ""),
        connect_timeout=10,
        send_receive_timeout=_TIMEOUT_S,
        settings={"join_use_nulls": 1},
    )
    try:
        return client.query(sql).result_rows
    finally:
        client.close()


def _scalar(rows) -> float:
    # First column of the first row as a float (None -> 0.0 so a missing table reads as a clean miss).
    if not rows or rows[0][0] is None:
        return 0.0
    return float(rows[0][0])


def fresh(max_lag_s: float) -> Callable[[float, float], bool]:
    # ch (CH max-timestamp epoch) is fresh if it trails ref (now()'s epoch) by no more than max_lag_s; ahead is fine.
    def cmp(ch: float, ref: float) -> bool:
        return (ref - ch) <= max_lag_s
    return cmp


def complete(eps: float) -> Callable[[float, float], bool]:
    # CH is "complete" vs the source (src) if it isn't short by more than eps. The source gate calls this with
    # eps=0 (EXACT lower bound, ch >= src) over a CLOSED window, so even one missing object trips it; the eps
    # param survives for callers/tests that want a tolerance. Surplus passes — a ReplacingMergeTree replay
    # over-reports raw count (the grain metric avoids that) and the gate only catches CH *missing* source data.
    def cmp(ch: float, src: float) -> bool:
        if src == 0:
            return True
        return ch >= src * (1 - eps)
    return cmp


def exact() -> Callable[[float, float], bool]:
    # Strict equality (ch == src), including when src == 0. The closed window makes CH and source identical when
    # complete, so a surplus is NOT tolerated — under `>=` a surplus could offset a missing row and pass. Counts
    # are integers well under 2^53, so float `==` is exact.
    def cmp(ch: float, src: float) -> bool:
        return ch == src
    return cmp


def run_parity(checks, label: str, *, ch_query=None, ref_query=None) -> dict:
    # Each check is (name, ch_sql, ref_sql, comparator); ch_sql runs on CH, ref_sql on the reference side
    # (the Garage Parquet via s3(), or wall-clock now()). Never raise: one bad check must not abort the rest.
    chq = ch_query or _ch_query
    refq = ref_query or _ch_query
    results = []
    for name, ch_sql, ref_sql, cmp in checks:
        try:
            ch_val = _scalar(chq(ch_sql))
            ref_val = _scalar(refq(ref_sql))
            ok = bool(cmp(ch_val, ref_val))
            err = None
        except Exception as e:  # a single bad check must not abort the rest
            ch_val = ref_val = float("nan")
            ok = False
            err = repr(e)
        results.append({"check": name, "ch": ch_val, "ref": ref_val, "ok": ok, "error": err})
        flag = "OK " if ok else "MISS"
        log.info("parity[%s] %s %-28s ch=%s ref=%s%s",
                 label, flag, name, ch_val, ref_val, f" err={err}" if err else "")

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    summary = {"label": label, "passed": passed, "total": total,
               "all_ok": passed == total, "results": results}
    level = logging.INFO if summary["all_ok"] else logging.WARNING
    log.log(level, "parity[%s] %d/%d checks within tolerance", label, passed, total)
    return summary


# --- Source-of-truth gate: CH vs the Garage Parquet (ground truth) -------------------------------
# CH bronze must not fall SHORT of the source Parquet (the data-loss failure mode); one in-flight ingest tick
# of trail is fine, a real load loss is far larger. Both sides run on the CH client (CH count vs s3() Parquet
# count), so this gate has no external-engine dependency.
def source_checks(cutoff: int) -> list[tuple[str, str, str, Callable[[float, float], bool]]]:
    # Build the completeness checks with a single captured `cutoff` epoch baked into BOTH sides of every window.
    # All EXACT (no eps). The metric per lane is the one that exactly counts distinct LEGIT source rows AND is
    # replay-immune: states = distinct content fp (collapses replays, keeps recaptures); adsb = (hex,capture_ts)
    # which is unique-per-row == content and replay-immune; flights/adsblol = raw count (no surplus to remove).
    dt = f"toDateTime({cutoff}, 'UTC')"
    return [
        ("bronze.opensky_states.content_fp",
         f"SELECT uniqExact({_STATES_CONTENT_FP}) FROM bronze.opensky_states WHERE snapshot_time < {dt}",
         f"SELECT uniqExact({_STATES_CONTENT_FP}) FROM s3({_GARAGE_COLLECTION}, "
         f"filename='bronze/{{states,states_raw}}/**/*.parquet', format='Parquet', structure='{_STATES_SRC_STRUCT}') "
         f"WHERE snapshot_time < {cutoff}",
         exact()),
        # (hex, capture_ts) is unique per row (== content), and uniqExact is replay-immune (v6.3 made adsb_states RMT).
        ("bronze.adsb_states.closed_grain",
         f"SELECT uniqExact((hex, capture_ts)) FROM bronze.adsb_states WHERE capture_ts < {cutoff}",
         f"SELECT uniqExact((hex, capture_ts)) FROM s3({_GARAGE_COLLECTION}, "
         f"filename='bronze/adsb_state/**/*.parquet', format='Parquet', structure='{_ADSB_SRC_STRUCT}') "
         f"WHERE capture_ts < {cutoff}",
         exact()),
        # flights: window on ingested_at, NOT first_seen — the daily 48h arrival lag decouples first_seen from load
        # time, so only ingested_at (source-frozen, on both sides) cleanly excludes the not-yet-loaded trail.
        ("bronze.opensky_flights.closed",
         f"SELECT count() FROM bronze.opensky_flights WHERE ingested_at < {dt}",
         f"SELECT count() FROM s3({_GARAGE_COLLECTION}, filename='bronze/flights_raw/**/*.parquet', format='Parquet', "
         f"structure='{_FLIGHTS_SRC_STRUCT}') WHERE parseDateTime64BestEffortOrNull(ingested_at) < {dt}",
         exact()),
        # adsblol: frozen one-time backfill, no trail -> exact raw count (no window needed).
        ("bronze.adsblol_states.exact",
         "SELECT count() FROM bronze.adsblol_states",
         f"SELECT count() FROM s3({_GARAGE_COLLECTION}, filename='bronze/adsblol_states_raw/**/*.parquet', "
         f"format='Parquet', structure='{_ADSBLOL_SRC_STRUCT}')",
         exact()),
    ]

# Freshness vs wall-clock (now()) — a stalled dbt CH lane would freeze these. Read as superset_ro so the gate
# also catches serving-credential drift (the only reason it stays a separate, serving-identity pass).
SOURCE_FRESHNESS_CHECKS: list[tuple[str, str, str, Callable[[float, float], bool]]] = [
    ("agg_hourly.freshness",
     "SELECT toUnixTimestamp(max(snapshot_hour)) FROM gold_ch.agg_hourly_traffic",
     "SELECT toUnixTimestamp(now())", fresh(_FRESHNESS_LAG_TOL_S)),
    ("fss.freshness",
     "SELECT toUnixTimestamp(max(snapshot_time)) FROM silver_ch.fact_state_snapshots",
     "SELECT toUnixTimestamp(now())", fresh(_FRESHNESS_LAG_TOL_S)),
    ("agg_country.freshness",
     "SELECT toUnixTimestamp(max(snapshot_ts)) FROM gold_ch.agg_country_traffic",
     "SELECT toUnixTimestamp(now())", fresh(_FRESHNESS_LAG_TOL_S)),
    # Rooftop ADS-B lane (transform_adsb_silver): capture_ts is Float64 epoch seconds — compare directly.
    ("fct_adsb_state.freshness",
     "SELECT max(capture_ts) FROM silver_ch.fct_adsb_state",
     "SELECT toUnixTimestamp(now())", fresh(_FRESHNESS_LAG_TOL_S)),
    # Flights lane (transform_flights): max(last_seen) — the daily ingest's D-0 departures keep this recent;
    # a 3-day lag means the lane stalled. (The O/D aggregates now build off fct_flights_reconciled — see below.)
    ("fact_flights.freshness",
     "SELECT toUnixTimestamp(max(last_seen)) FROM gold_ch.fact_flights",
     "SELECT toUnixTimestamp(now())", fresh(_FLIGHTS_FRESHNESS_LAG_TOL_S)),
    # Reconciled consensus mart (SP2) — built by transform_marts. Covers the O/D-derived aggregates
    # (agg_route_traffic / agg_operator_traffic / longest_flights / agg_airport_daily) transitively (same build).
    ("fct_flights_reconciled.freshness",
     "SELECT toUnixTimestamp(max(end_time)) FROM gold_ch.fct_flights_reconciled",
     "SELECT toUnixTimestamp(now())", fresh(_FLIGHTS_FRESHNESS_LAG_TOL_S)),
    # anomalies is intentionally excluded: it's a sparse/filtered mart, so max(snapshot_time) is the last anomaly
    # (legitimately hours old on a calm sky) — not a build-freshness signal. Its data presence is covered by the
    # bronze completeness checks + fss.freshness (the states pipeline it derives from).
]


def _broken_parts_check(ch_query) -> dict:
    # Own check (not a run_parity comparator: the query returns part names, not a scalar) so the RuntimeError
    # can name the actual broken part(s) — the point is the operator sees WHICH table/part broke on the next tick.
    name = "source.no_broken_parts"
    try:
        parts = [r[0] for r in ch_query(_BROKEN_PARTS_SQL)]
        ok, err = not parts, None
    except Exception as e:  # a bad tripwire query must not abort the rest of the gate
        parts, ok, err = [], False, repr(e)
    if parts:
        log.warning("parity[source] broken-on-start detached parts found: %s", parts)
    elif err:
        log.warning("parity[source] broken-parts tripwire query failed: %s", err)
    return {"check": name, "ch": len(parts), "ref": 0, "ok": ok, "error": err, "parts": parts}


def run_source_gate(*, ch_query=None, serving_query=None) -> dict:
    # The standing serving guard: CH bronze is complete vs the source Parquet, the served marts are fresh, AND
    # no _acc part crash-detached silently (#116). Completeness + the tripwire run on the admin client (needs
    # bronze + the garage s3() collection); freshness runs as superset_ro (the serving identity) to also catch
    # credential drift. Raises on any miss.
    chq = ch_query or _ch_query
    sq = serving_query or _ch_query_serving
    comp = run_parity(source_checks(_closed_cutoff()), "source.complete", ch_query=chq, ref_query=chq)
    frsh = run_parity(SOURCE_FRESHNESS_CHECKS, "source.fresh", ch_query=sq, ref_query=sq)
    broken = _broken_parts_check(chq)
    results = comp["results"] + frsh["results"] + [broken]
    passed = comp["passed"] + frsh["passed"] + (1 if broken["ok"] else 0)
    total = comp["total"] + frsh["total"] + 1
    summary = {"label": "source", "passed": passed, "total": total,
               "all_ok": passed == total, "results": results}
    if not summary["all_ok"]:
        fails = [r["check"] for r in results if not r["ok"]]
        # A tripwire QUERY failure (not a real broken part) must still be diagnosable, not just an anonymous red.
        if broken["parts"]:
            detail = f"; broken parts: {broken['parts'][:5]}"
        elif broken["error"]:
            detail = f"; broken-parts query error: {broken['error']}"
        else:
            detail = ""
        raise RuntimeError(f"CH source gate FAILED ({len(fails)}/{total}): {fails}{detail}")
    return summary


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(run_source_gate(), default=str, indent=2))
