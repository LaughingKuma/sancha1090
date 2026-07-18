from __future__ import annotations

import logging
import time
from typing import Optional

import sqlalchemy as sa

from include.ch_parity import _CLOSED_WINDOW_S, _ch_query
from include.db import analytics_engine

log = logging.getLogger(__name__)

# The hour-bucketed window math (cutoff = top-of-hour - _CLOSED_WINDOW_S) only stays hour-aligned if the closed
# window is a whole number of hours — a misaligned value would false-mismatch every boundary hour (oracle buckets
# a partial hour while the mart carries the full one).
assert _CLOSED_WINDOW_S % 3600 == 0, "_CLOSED_WINDOW_S must be a whole number of hours"

# Per-hour served-VALUE gate: the completeness gate proves bronze == source and freshness proves the lane moves,
# but neither sees a transform that mis-aggregates correct bronze OR an accumulate-forever MV that silently drops
# a historical hour (catchup=False never revisits it). This validates each newly-closed hour mart-vs-bronze.
# Oracle = bronze.opensky_states (NOT the source Parquet directly): it is ORDER BY (snapshot_time, …) so a per-hour
# read is exact AND PK-pruned, holds every row regardless of the Parquet ingest-tick partition skew (unbounded via
# backfill), and is independently proven == source by the completeness gate — so mart==bronze ∧ bronze==source ⇒
# mart==source transitively.
_GATE = "states_hourly"
# The MV's exact filter (mirrors include/regions.py / stg_states.sql / ch_incremental_mvs._GEO, hand-synced) — the
# oracle must read the SAME geo population the mart aggregates, or it compares two different row sets.
_GEO = "latitude BETWEEN 20 AND 50 AND longitude BETWEEN 122 AND 165 AND latitude IS NOT NULL AND longitude IS NOT NULL"
# Re-validate this many hours below the watermark each run: a late bronze insert the MV missed (broken/detached MV)
# would otherwise pass once and never be revisited (catchup=False + no reseed once the watermark advances).
_LATENESS_S = 6 * 3600
# The two re-grained ADS-B _acc tables carry a 90d TTL; stay a day inside it (TTL drops lazily on merge) so the
# gate never compares a (group,hour) the _acc may already have aged out.
_ADSB_TTL_VALID_S = 89 * 24 * 3600
# Validate at most this much window per run. The catch-up is unbounded over time, but the CH client has a hard
# 60s timeout (_TIMEOUT_S) — one oversized post-outage scan would time out, red the task, and never advance the
# watermark, so the next run retries the same doomed scan forever. Draining a bounded chunk per run (advancing the
# watermark to each validated boundary) clears the backlog over successive runs.
_CATCHUP_CHUNK_S = 2 * 24 * 3600

# Both must be whole hours (like _CLOSED_WINDOW_S) so win_start/chunk_end land on hour boundaries — a misaligned
# bound would compare a partial oracle hour against the mart's full hour and false-mismatch it. And the chunk must
# clear the re-validated lateness tail + 1h, or a backlog never drains (each run re-scans the same window).
assert _LATENESS_S % 3600 == 0 and _CATCHUP_CHUNK_S % 3600 == 0, "_LATENESS_S/_CATCHUP_CHUNK_S must be whole hours"
assert _CATCHUP_CHUNK_S > _LATENESS_S + 3600, "_CATCHUP_CHUNK_S must exceed _LATENESS_S + 1h for net catch-up progress"

# Seam: tests point this at a schema-less sqlite mirror; production uses the public schema (mirrors manifest.py).
_WM_TABLE = "public.ch_served_value_audit"

_default_engine: Optional[sa.Engine] = None
_table_ready = False


def _engine() -> sa.Engine:
    global _default_engine
    if _default_engine is None:
        _default_engine = analytics_engine()
    return _default_engine


def _ensure_table(engine: Optional[sa.Engine] = None) -> None:
    global _table_ready
    eng = engine or _engine()
    with eng.begin() as conn:
        conn.execute(sa.text(
            f"CREATE TABLE IF NOT EXISTS {_WM_TABLE} ("
            f" gate TEXT PRIMARY KEY,"
            f" watermark_hour BIGINT NOT NULL,"
            f" updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        ))
    if engine is None:
        _table_ready = True


def get_watermark(gate: str = _GATE, engine: Optional[sa.Engine] = None) -> Optional[int]:
    eng = engine or _engine()
    if engine is None and not _table_ready:
        _ensure_table()
    with eng.begin() as conn:
        row = conn.execute(
            sa.text(f"SELECT watermark_hour FROM {_WM_TABLE} WHERE gate = :g"), {"g": gate}
        ).fetchone()
    return int(row[0]) if row else None


def set_watermark(value: int, gate: str = _GATE, engine: Optional[sa.Engine] = None) -> None:
    # Advance-only (read-then-write, portable across postgres/sqlite — no ON CONFLICT/GREATEST): a recomputed-low
    # value (clock skew, a re-run) must never roll the watermark backward and re-skip already-validated hours.
    eng = engine or _engine()
    if engine is None and not _table_ready:
        _ensure_table()
    with eng.begin() as conn:
        cur = conn.execute(
            sa.text(f"SELECT watermark_hour FROM {_WM_TABLE} WHERE gate = :g"), {"g": gate}
        ).fetchone()
        if cur is None:
            conn.execute(
                sa.text(f"INSERT INTO {_WM_TABLE} (gate, watermark_hour) VALUES (:g, :v)"),
                {"g": gate, "v": value},
            )
        elif value > int(cur[0]):
            conn.execute(
                sa.text(
                    f"UPDATE {_WM_TABLE} SET watermark_hour = :v, updated_at = CURRENT_TIMESTAMP WHERE gate = :g"
                ),
                {"g": gate, "v": value},
            )


def _dt(epoch: int) -> str:
    return f"toDateTime({epoch}, 'UTC')"


def _rows_to_map(rows, n: int):
    # Keyed by hour-epoch (col 0); value = the single metric (n==1) or a tuple of metrics. Skip a NULL hour key.
    out = {}
    for r in rows or []:
        if r[0] is None:
            continue
        out[int(r[0])] = float(r[1]) if n == 1 else tuple(float(c) for c in r[1 : 1 + n])
    return out


def _adsb_value_checks(chq, win_start: int, chunk_end: int, last_closed: int) -> list:
    # Per-(group, hour) served-value checks for the two re-grained ADS-B MVs, exact vs a bronze.adsb_states oracle.
    # capture_ts is Float64 epoch sec -> BARE int window bounds (NOT toDateTime, unlike the opensky oracle); the
    # adsb MVs have NO geo filter (they aggregate all bronze). The oracle reuses the MV's OWN dict/military/airline
    # exprs (include.ch_incremental_mvs) so it reads the exact population each MV aggregates. The _acc reads window
    # snapshot_hour (a DateTime) -> toDateTime bounds. Gated behind the completeness gate (gate >> value_gate), so
    # mart==bronze ∧ bronze==source ⇒ mart==source.
    from include import ch_incremental_mvs as _mv

    floor = last_closed - _ADSB_TTL_VALID_S
    lo = max(win_start, floor)
    if lo >= chunk_end:
        return []  # whole window below the TTL floor: the _acc has aged it out by design
    dt_lo, dt_hi = _dt(lo), _dt(chunk_end)

    # 1) Country — distinct_aircraft + observations + military, per (reg_country, hour). Clean exact (no backfill).
    c_oracle = {(r[0], int(r[1])): (int(r[2]), int(r[3]), int(r[4])) for r in chq(
        f"SELECT reg_country, toUnixTimestamp(toStartOfHour(toDateTime(capture_ts))) h, "
        f"uniqExact(hex), uniqExact((hex, capture_ts)), uniqExactIf((hex, capture_ts), {_mv._IS_MILITARY}) "
        f"FROM (SELECT {_mv._HEX_COUNTRY} AS reg_country, hex, capture_ts, db_flags FROM bronze.adsb_states "
        f"      WHERE capture_ts >= {lo} AND capture_ts < {chunk_end}) "
        f"WHERE reg_country IS NOT NULL GROUP BY reg_country, h") if r[0] is not None and r[1] is not None}
    c_mart = {(r[0], int(r[1])): (int(r[2]), int(r[3]), int(r[4])) for r in chq(
        f"SELECT reg_country, toUnixTimestamp(snapshot_hour) h, uniqExactMerge(distinct_aircraft_state), "
        f"uniqExactMerge(observations), uniqExactMerge(military_observations) "
        f"FROM gold_ch.agg_country_traffic_adsb_acc WHERE snapshot_hour >= {dt_lo} AND snapshot_hour < {dt_hi} "
        f"GROUP BY reg_country, h") if r[0] is not None and r[1] is not None}
    c_keys = sorted(set(c_oracle) | set(c_mart))
    c_mm = [{"group": k[0], "hour": k[1], "oracle": list(c_oracle.get(k, (0, 0, 0))), "mart": list(c_mart.get(k, (0, 0, 0)))}
            for k in c_keys if c_oracle.get(k, (0, 0, 0)) != c_mart.get(k, (0, 0, 0))]

    # 2) Airline — distinct_aircraft + observations + backfilled, per (airline, hour). Exact: the oracle reproduces
    # the MV's FULL (hex,capture_ts)->airline attribution (two-sided OpenSky callsign backfill; argMinIf == the seed
    # ASOF, verified diff-0), so the OpenSky-backfilled portion AND distinct_aircraft are validated too — not just
    # the native subset (which the bronze completeness gate alone can't protect against a transform/MV defect).
    a_oracle = {(r[0], r[1], int(r[2])): (int(r[3]), int(r[4]), int(r[5])) for r in chq(
        _mv.adsb_airline_oracle_sql(lo, chunk_end)) if r[0] is not None and r[2] is not None}
    a_mart = {(r[0], r[1], int(r[2])): (int(r[3]), int(r[4]), int(r[5])) for r in chq(
        f"SELECT airline_name, airline_country, toUnixTimestamp(snapshot_hour) h, uniqExactMerge(distinct_aircraft_state), "
        f"uniqExactMerge(observations), uniqExactMerge(backfilled_observations) "
        f"FROM gold_ch.agg_airline_traffic_adsb_acc WHERE snapshot_hour >= {dt_lo} AND snapshot_hour < {dt_hi} "
        f"GROUP BY airline_name, airline_country, h") if r[0] is not None and r[2] is not None}
    a_keys = sorted(set(a_oracle) | set(a_mart))
    a_mm = [{"group": [k[0], k[1]], "hour": k[2], "oracle": list(a_oracle.get(k, (0, 0, 0))), "mart": list(a_mart.get(k, (0, 0, 0)))}
            for k in a_keys if a_oracle.get(k, (0, 0, 0)) != a_mart.get(k, (0, 0, 0))]

    return [
        {"check": "agg_country_traffic_adsb.value", "hours": len(c_keys), "ok": not c_mm, "mismatches": c_mm[:8]},
        {"check": "agg_airline_traffic_adsb.value", "hours": len(a_keys), "ok": not a_mm, "mismatches": a_mm[:8]},
    ]


def run_value_gate(*, ch_query=None, get_wm=None, set_wm=None, now_epoch=None) -> dict:
    # For every newly-closed hour (and a lateness tail below the watermark), assert the served marts equal the
    # bronze oracle for that hour. Advance the watermark only on a full pass; raise on any mismatch.
    chq = ch_query or _ch_query
    getwm = get_wm or get_watermark
    setwm = set_wm or set_watermark
    now = now_epoch if now_epoch is not None else int(time.time())

    # Same closed boundary the completeness gate uses, so both gates agree on what "loaded" means. The newest fully
    # closed hour ends at the cutoff; anything at/after it is still in-flight.
    cutoff = now // 3600 * 3600 - _CLOSED_WINDOW_S
    last_closed = cutoff - 3600

    wm = getwm()
    # Catch up EVERY hour held since the watermark — unbounded, so a long completeness outage can't drop held
    # hours once it recovers (value_gate is gated behind the source gate, so it freezes rather than advances while
    # bronze is short — the watermark therefore stays recent and the catch-up never reaches the live opensky_states
    # floor / archive-seed region). First run (no watermark) validates only the recent lateness window: pre-deploy
    # history is the baseline-reconcile's job, and scanning below the live floor would false-mismatch the seed.
    anchor = last_closed if wm is None else wm
    win_start = anchor - _LATENESS_S
    # One bounded chunk per run; the watermark advances to its boundary so a backlog drains across runs.
    chunk_end = min(cutoff, win_start + _CATCHUP_CHUNK_S)

    results = []
    oracle = _rows_to_map(chq(
        f"SELECT toUnixTimestamp(toStartOfHour(snapshot_time)) h, "
        f"uniqExact((icao24, snapshot_time)) obs, uniqExact(icao24) ac "
        f"FROM bronze.opensky_states "
        f"WHERE {_GEO} AND snapshot_time >= {_dt(win_start)} AND snapshot_time < {_dt(chunk_end)} "
        f"GROUP BY h"), 2)
    agg = _rows_to_map(chq(
        f"SELECT toUnixTimestamp(snapshot_hour) h, total_observations obs, unique_aircraft ac "
        f"FROM gold_ch.agg_hourly_traffic "
        f"WHERE snapshot_hour >= {_dt(win_start)} AND snapshot_hour < {_dt(chunk_end)}"), 2)
    fss = _rows_to_map(chq(
        f"SELECT toUnixTimestamp(toStartOfHour(snapshot_time)) h, count() obs "
        f"FROM silver_ch.fact_state_snapshots "
        f"WHERE snapshot_time >= {_dt(win_start)} AND snapshot_time < {_dt(chunk_end)} "
        f"GROUP BY h"), 1)

    # Union of hour keys so a phantom mart hour (present in the mart, zero in the oracle) reds too, not just a drop.
    hours = sorted(set(oracle) | set(agg) | set(fss))
    agg_mm, fss_mm = [], []
    for h in hours:
        o_obs, o_ac = oracle.get(h, (0.0, 0.0))
        a_obs, a_ac = agg.get(h, (0.0, 0.0))
        if a_obs != o_obs or a_ac != o_ac:
            agg_mm.append({"hour": h, "mart": [a_obs, a_ac], "oracle": [o_obs, o_ac]})
        if fss.get(h, 0.0) != o_obs:
            fss_mm.append({"hour": h, "mart": fss.get(h, 0.0), "oracle": o_obs})

    results.append({"check": "agg_hourly_traffic.value", "hours": len(hours),
                    "ok": not agg_mm, "mismatches": agg_mm[:8]})
    results.append({"check": "fact_state_snapshots.value", "hours": len(hours),
                    "ok": not fss_mm, "mismatches": fss_mm[:8]})
    # The two now-exact ADS-B MVs (re-grained off HLL in v6.3) — same window + watermark (capture_ts shares
    # wall-clock with snapshot_time), so a defect there freezes the same watermark until it's fixed.
    results.extend(_adsb_value_checks(chq, win_start, chunk_end, last_closed))
    for r in results:
        log.log(logging.INFO if r["ok"] else logging.WARNING,
                "value[%s] %s hours=%d mismatches=%d", r["check"],
                "OK " if r["ok"] else "MISS", r["hours"], len(r["mismatches"]))

    passed = sum(1 for r in results if r["ok"])
    all_ok = passed == len(results)
    summary = {"label": "value", "passed": passed, "total": len(results),
               "all_ok": all_ok, "window": [win_start, chunk_end], "results": results}
    if not all_ok:
        fails = [r["check"] for r in results if not r["ok"]]
        samples = {r["check"]: r["mismatches"][:3] for r in results if not r["ok"]}
        raise RuntimeError(f"CH served-value gate FAILED ({len(fails)}/{len(results)}): {fails}; samples={samples}")
    # Advance to this chunk's last fully-closed hour; a remaining backlog drains on the next run.
    setwm(chunk_end - 3600)
    return summary


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(run_value_gate(), default=str, indent=2))
