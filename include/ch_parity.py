from __future__ import annotations

import logging
import os
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
# CH bronze is "complete" if it isn't short of the source Parquet by more than this.
_SOURCE_EPS = 0.02


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
    # CH bronze is "complete" vs the source Parquet (src) if it isn't short by more than eps — one in-flight
    # ingest tick can leave CH a hair behind the landing zone, but a real load loss is far larger. Surplus
    # (e.g. the opensky_states P2 dup) passes — this gate only catches CH *missing* source data.
    def cmp(ch: float, src: float) -> bool:
        if src == 0:
            return True
        return ch >= src * (1 - eps)
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
SOURCE_CHECKS: list[tuple[str, str, str, Callable[[float, float], bool]]] = [
    ("bronze.opensky_flights.complete",
     "SELECT count() FROM bronze.opensky_flights",
     f"SELECT count() FROM s3({_GARAGE_COLLECTION}, filename='bronze/flights_raw/**/*.parquet', format='Parquet')",
     complete(_SOURCE_EPS)),
    ("bronze.adsb_states.complete",
     "SELECT count() FROM bronze.adsb_states",
     f"SELECT count() FROM s3({_GARAGE_COLLECTION}, filename='bronze/adsb_state/**/*.parquet', format='Parquet')",
     complete(_SOURCE_EPS)),
    ("bronze.opensky_states.complete",
     "SELECT count() FROM bronze.opensky_states",
     f"SELECT count() FROM s3({_GARAGE_COLLECTION}, filename='bronze/{{states,states_raw}}/**/*.parquet', format='Parquet')",
     complete(_SOURCE_EPS)),
    ("bronze.archive_states.complete",
     "SELECT count() FROM bronze.archive_states",
     f"SELECT count() FROM s3({_GARAGE_COLLECTION}, filename='bronze/archive_states_raw/**/*.parquet', format='Parquet')",
     complete(_SOURCE_EPS)),
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
    # a 3-day lag means the lane stalled. Covers agg_airport_daily / agg_flight_routes transitively (same build).
    ("fact_flights.freshness",
     "SELECT toUnixTimestamp(max(last_seen)) FROM gold_ch.fact_flights",
     "SELECT toUnixTimestamp(now())", fresh(_FLIGHTS_FRESHNESS_LAG_TOL_S)),
    # anomalies is intentionally excluded: it's a sparse/filtered mart, so max(snapshot_time) is the last anomaly
    # (legitimately hours old on a calm sky) — not a build-freshness signal. Its data presence is covered by the
    # bronze completeness checks + fss.freshness (the states pipeline it derives from).
]


def run_source_gate(*, ch_query=None, serving_query=None) -> dict:
    # The standing serving guard: CH bronze is complete vs the source Parquet AND the served marts are fresh.
    # Completeness runs on the admin client (needs bronze + the garage s3() collection); freshness runs as
    # superset_ro (the serving identity) to also catch credential drift. Raises on any miss.
    chq = ch_query or _ch_query
    sq = serving_query or _ch_query_serving
    comp = run_parity(SOURCE_CHECKS, "source.complete", ch_query=chq, ref_query=chq)
    frsh = run_parity(SOURCE_FRESHNESS_CHECKS, "source.fresh", ch_query=sq, ref_query=sq)
    results = comp["results"] + frsh["results"]
    passed = comp["passed"] + frsh["passed"]
    total = comp["total"] + frsh["total"]
    summary = {"label": "source", "passed": passed, "total": total,
               "all_ok": passed == total, "results": results}
    if not summary["all_ok"]:
        fails = [r["check"] for r in results if not r["ok"]]
        raise RuntimeError(f"CH source gate FAILED ({len(fails)}/{total}): {fails}")
    return summary


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(run_source_gate(), default=str, indent=2))
