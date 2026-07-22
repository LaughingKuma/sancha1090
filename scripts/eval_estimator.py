import argparse
import importlib.util
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from itertools import pairwise
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("estimator", REPO_ROOT / "livemap" / "estimator.py")
est = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(est)

CH_URL = os.environ.get("EVAL_CH_URL", "http://127.0.0.1:38123")
CH_USER = os.environ.get("EVAL_CH_USER", "default")
CH_PASSWORD = os.environ.get("EVAL_CH_PASSWORD", "")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def ch_query(sql):
    q = urllib.parse.urlencode({"default_format": "JSON"})
    req = urllib.request.Request(f"{CH_URL}/?{q}", data=sql.encode(),
                                 headers={"X-ClickHouse-User": CH_USER,
                                          "X-ClickHouse-Key": CH_PASSWORD})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["data"]


def select_truth_flights(day_lo, day_hi, per_stratum=150):
    if not (DAY_RE.match(day_lo) and DAY_RE.match(day_hi)):
        raise ValueError(f"day args must be YYYY-MM-DD: {day_lo!r}, {day_hi!r}")
    return ch_query(f"""
        SELECT * FROM (
            SELECT p.flight_id AS flight_id, toString(p.day_key_any) AS day_key,
                   r.origin_lat, r.origin_lon, r.origin_source, r.origin_agreement,
                   r.dest_lat, r.dest_lon, r.dest_source, r.dest_agreement,
                   row_number() OVER (PARTITION BY r.origin_source, r.origin_agreement,
                                      r.dest_source, r.dest_agreement,
                                      multiIf(p.in_china = 1, 'china', p.in_pacific = 1, 'pacific', 'other')
                                      ORDER BY p.flight_id) AS rn
            FROM (
                SELECT flight_id, any(day_key) AS day_key_any,
                       arrayMax(arrayDifference(arraySort(groupArray(toUnixTimestamp(ts))))) AS max_gap,
                       argMin((lat, lon), ts) AS first_pt, argMax((lat, lon), ts) AS last_pt,
                       argMin(coalesce(alt_ft, 99999.0), ts) AS first_alt,
                       argMin(on_ground, ts) AS first_ground,
                       argMax(coalesce(alt_ft, 99999.0), ts) AS last_alt,
                       argMax(on_ground, ts) AS last_ground,
                       countIf(lon BETWEEN {CHINA_LON_LO} AND {CHINA_LON_HI}) > 0 AS in_china,
                       (max(lon) >= 150.0 OR min(lon) <= -150.0) AS in_pacific
                FROM gold_ch.fct_flight_path
                WHERE day_key BETWEEN '{day_lo}' AND '{day_hi}'
                GROUP BY flight_id
                HAVING max_gap < 300
            ) p
            JOIN gold_ch.fct_flights_reconciled r ON r.flight_id = p.flight_id
            WHERE r.origin_lat IS NOT NULL AND r.dest_lat IS NOT NULL
              AND r.origin_lon IS NOT NULL AND r.dest_lon IS NOT NULL
              AND (p.first_alt < 8000 OR p.first_ground = 1)
              AND (p.last_alt < 8000 OR p.last_ground = 1)
              AND geoDistance(p.first_pt.2, p.first_pt.1, r.origin_lon, r.origin_lat) < 20000
              AND geoDistance(p.last_pt.2, p.last_pt.1, r.dest_lon, r.dest_lat) < 20000
        ) WHERE rn <= {int(per_stratum)}
        ORDER BY flight_id
        SETTINGS join_use_nulls = 1
    """)


def fetch_points(flight_id):
    rows = ch_query(f"""
        SELECT toUnixTimestamp(ts) AS ts, lat, lon, alt_ft, on_ground, gs_kt, track_deg, source
        FROM gold_ch.fct_flight_path WHERE flight_id = {int(flight_id)} ORDER BY ts
    """)
    return [(r["ts"], r["lat"], r["lon"], r["alt_ft"], bool(r["on_ground"]),
             r["gs_kt"], r["track_deg"], r["source"]) for r in rows]


def mask_terminal(points, seconds):
    cut = points[-1][0] - seconds
    kept = [p for p in points if p[0] <= cut]
    return kept, [p for p in points if p[0] > cut]


def mask_leading(points, seconds):
    cut = points[0][0] + seconds
    kept = [p for p in points if p[0] >= cut]
    return kept, [p for p in points if p[0] < cut]


def mask_window(points, start_frac, seconds):
    t0, t1 = points[0][0], points[-1][0]
    ws = t0 + (t1 - t0) * start_frac
    kept = [p for p in points if not (ws <= p[0] < ws + seconds)]
    return kept, [p for p in points if ws <= p[0] < ws + seconds]


def mask_lonbox(points, lon_lo, lon_hi):
    kept = [p for p in points if not (lon_lo <= p[2] <= lon_hi)]
    return kept, [p for p in points if lon_lo <= p[2] <= lon_hi]


WINDOW_DURATIONS = [1200.0, 2700.0, 5400.0, 10800.0]
MARGIN_S = 900.0
TERMINAL_MASK_S = 2400.0
ERR_SAMPLE_MAX = 20
CHINA_LON_LO, CHINA_LON_HI = 100.0, 125.0
TARGET_KINDS = {"terminal": "dest_ext", "leading": "origin_ext", "window": "gap",
                "dr": "dr", "lonbox": "gap"}


def interp_at(seg_points, ts):
    # linear between 60 s neighbors; harness scenarios stay far from the antimeridian
    for p1, p2 in pairwise(seg_points):
        if p1[2] <= ts <= p2[2]:
            if p2[2] == p1[2]:
                return (p1[1], p1[0])
            f = (ts - p1[2]) / (p2[2] - p1[2])
            return (p1[1] + (p2[1] - p1[1]) * f, p1[0] + (p2[0] - p1[0]) * f)
    return None


def pos_error_km(result, truth_point, kind):
    ts, lat, lon = truth_point[0], truth_point[1], truth_point[2]
    for seg in result.segments:
        if seg.kind != kind:
            continue
        got = interp_at(seg.points, ts)
        if got is not None:
            return est.haversine_nm(lat, lon, got[0], got[1]) * est.KM_PER_NM
    return None


def eta_error_s(result, masked):
    for seg in result.segments:
        if seg.kind != "dest_ext":
            continue
        end = seg.points[-1]
        truth = min(masked, key=lambda p: est.haversine_nm(p[1], p[2], end[1], end[0]))
        return end[2] - truth[0]
    return None


def _region(points):
    # touch-based (a midpoint test misses box-touchers); must match the SQL selection predicate
    lons = [p[2] for p in points]
    if any(CHINA_LON_LO <= x <= CHINA_LON_HI for x in lons):
        return "china"
    if any(x >= 150.0 or x <= -150.0 for x in lons):
        return "pacific"
    return "other"


def _lonbox_runs(points):
    # visits must be independently bounded so truncation and separate crossings are never
    # conflated into one bridge-eligibility experiment
    runs, cur = [], []
    for i, p in enumerate(points):
        if CHINA_LON_LO <= p[2] <= CHINA_LON_HI:
            cur.append(i)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    out = []
    for run in runs:
        if run[0] == 0 or run[-1] == len(points) - 1 or len(run) < 3:
            continue
        gap_dur = points[run[-1] + 1][0] - points[run[0] - 1][0]
        if gap_dur <= est.DEFAULT_CONFIG.gap_min_s:
            continue
        out.append((points[:run[0]] + points[run[-1] + 1:], points[run[0]:run[-1] + 1], gap_dur))
    return out


def _mask_for(flight_row, points, scenario):
    dur = points[-1][0] - points[0][0]
    if scenario in ("terminal", "dr"):
        if dur < TERMINAL_MASK_S + 2 * MARGIN_S:
            return None
        return mask_terminal(points, TERMINAL_MASK_S)
    if scenario == "leading":
        if dur < TERMINAL_MASK_S + 2 * MARGIN_S:
            return None
        return mask_leading(points, TERMINAL_MASK_S)
    fitting = [w for w in WINDOW_DURATIONS if dur >= w + 2 * MARGIN_S]
    if not fitting:
        return None
    win = fitting[int(flight_row["flight_id"]) % len(fitting)]
    return mask_window(points, ((dur - win) / 2.0) / dur, win)   # centered: truth both sides


def _od_for(flight_row, scenario):
    origin = est.Endpoint(flight_row["origin_lat"], flight_row["origin_lon"],
                          flight_row.get("origin_source"), flight_row.get("origin_agreement"))
    dest = est.Endpoint(flight_row["dest_lat"], flight_row["dest_lon"],
                        flight_row.get("dest_source"), flight_row.get("dest_agreement"))
    if scenario == "dr":
        dest = est.Endpoint()   # hidden destination -> the NULL-dest DR trigger fires
    return est.OD(origin=origin, dest=dest)


def _endpoint_fields(flight_row, kind, scenario):
    if kind == "origin_ext":
        return flight_row.get("origin_source"), flight_row.get("origin_agreement")
    if kind == "dest_ext" and scenario != "dr":
        return flight_row.get("dest_source"), flight_row.get("dest_agreement")
    return None, None


def evaluate_flight(flight_row, points, scenario):
    if scenario == "lonbox":
        rows = []
        for kept, masked, gap_dur in _lonbox_runs(points):
            rows.extend(_rows_for_mask(flight_row, points, scenario, kept, masked, gap_dur))
        return rows
    masked_pair = _mask_for(flight_row, points, scenario)
    if masked_pair is None:
        return []
    kept, masked = masked_pair
    if len(kept) < 1 or len(masked) < 3:
        return []
    return _rows_for_mask(flight_row, points, scenario, kept, masked)


def _rows_for_mask(flight_row, points, scenario, kept, masked, gap_dur=None):
    target = TARGET_KINDS[scenario]
    r = est.estimate(kept, _od_for(flight_row, scenario))
    src, agr = _endpoint_fields(flight_row, target, scenario)
    base = {"flight_id": flight_row["flight_id"], "scenario": scenario, "target_kind": target,
            "region": _region(points), "source": src, "agreement": agr}
    rows = []
    target_segs = [s for s in r.segments if s.kind == target]
    for seg in target_segs:
        score_pts = masked
        if scenario == "dr":
            horizon = kept[-1][0] + est.DEFAULT_CONFIG.dr_cap_s
            score_pts = [p for p in masked if p[0] <= horizon]
        if len(score_pts) <= ERR_SAMPLE_MAX:
            sampled = score_pts
        else:
            # endpoint-spanning: stride+truncate sampled only a prefix, flattering late-horizon error
            step = (len(score_pts) - 1) / (ERR_SAMPLE_MAX - 1)
            sampled = [score_pts[round(i * step)] for i in range(ERR_SAMPLE_MAX)]
        errs = [pos_error_km(r, p, target) for p in sampled]
        scored = [e for e in errs if e is not None]
        rows.append({**base, "bin": seg.meta["bin"], "eligible": bool(scored),
                     "coverage": len(scored) / len(sampled) if sampled else 0.0,
                     "errors": scored,
                     "eta_s": eta_error_s(r, masked) if target == "dest_ext" else None,
                     "skip_reason": None})
    if not target_segs:
        reasons = [s["reason"] for s in r.skips if s["kind"] == target]
        if scenario == "window":
            dur = points[-1][0] - points[0][0]
            fitting = [w for w in WINDOW_DURATIONS if dur >= w + 2 * MARGIN_S]
            win = fitting[int(flight_row["flight_id"]) % len(fitting)]
            # a-priori bin (+60 s mask cadence) keeps a rejected row in the stratum it failed out of
            skip_bin = est._gap_bin(win + 60.0)
        elif scenario == "lonbox":
            skip_bin = est._gap_bin(gap_dur)
        else:
            skip_bin = target
        rows.append({**base, "bin": skip_bin, "eligible": False, "coverage": 0.0, "errors": [],
                     "eta_s": None, "skip_reason": reasons[0] if reasons else "not_produced"})
    return rows


def pctl(values, q):
    # nearest-rank: ceil(q% * n), 1-indexed — deterministic, no banker's rounding
    vals = sorted(values)
    idx = max(1, math.ceil(q / 100.0 * len(vals))) - 1
    return vals[min(idx, len(vals) - 1)]


def summarize(rows):
    strata = {}
    for r in rows:
        key = f"{r['scenario']}|{r['target_kind']}|{r['bin']}|{r['source']}|{r['agreement']}|{r['region']}"
        s = strata.setdefault(key, {"n": 0, "eligible": 0, "errs": [], "eta": []})
        s["n"] += 1
        if r["eligible"]:
            s["eligible"] += 1
            s["errs"].extend(r["errors"])   # point-level pool: the p90 is a true p90
            if r["eta_s"] is not None:
                s["eta"].append(abs(r["eta_s"]))
    out = {}
    for key, s in strata.items():
        out[key] = {"n": s["n"], "eligibility": s["eligible"] / s["n"],
                    "pos_p50_km": pctl(s["errs"], 50) if s["errs"] else None,
                    "pos_p90_km": pctl(s["errs"], 90) if s["errs"] else None,
                    "eta_p50_s": pctl(s["eta"], 50) if s["eta"] else None,
                    "eta_p90_s": pctl(s["eta"], 90) if s["eta"] else None}
    return out


def skip_table(rows):
    out = {}
    for r in rows:
        if r["skip_reason"]:
            key = (r["target_kind"], r["skip_reason"])
            out[key] = out.get(key, 0) + 1
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day-lo", required=True)
    ap.add_argument("--day-hi", required=True)
    ap.add_argument("--per-stratum", type=int, default=150)
    ap.add_argument("--scenarios", default="terminal,leading,window,dr,lonbox")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    scenarios = args.scenarios.split(",")
    unknown = sorted(set(scenarios) - set(TARGET_KINDS))
    if unknown:
        raise SystemExit(f"unknown scenarios {unknown}; valid: {sorted(TARGET_KINDS)}")
    flights = select_truth_flights(args.day_lo, args.day_hi, args.per_stratum)
    rows, mask_yield = [], {}
    for fr in flights:
        points = fetch_points(fr["flight_id"])
        for sc in scenarios:
            got = evaluate_flight(fr, points, sc)
            y = mask_yield.setdefault(sc, {"candidates": 0, "masked_ok": 0})
            y["candidates"] += 1
            if got:
                y["masked_ok"] += 1
            rows.extend(got)
    strata = summarize(rows)
    skips = {f"{k}|{reason}": v for (k, reason), v in skip_table(rows).items()}
    Path(args.out).write_text(json.dumps(
        {"strata": strata, "skips": skips, "mask_yield": mask_yield, "rows": rows}, default=str))
    print("| stratum | n | elig | pos p50 km | pos p90 km | eta p50 s | eta p90 s |")
    print("|---|---|---|---|---|---|---|")
    for key in sorted(strata):
        s = strata[key]
        print(f"| {key} | {s['n']} | {s['eligibility']:.2f} | {s['pos_p50_km']} "
              f"| {s['pos_p90_km']} | {s['eta_p50_s']} | {s['eta_p90_s']} |")
    print()
    print("| target kind | skip reason | n |")
    print("|---|---|---|")
    for (k, reason), v in sorted(skip_table(rows).items()):
        print(f"| {k} | {reason} | {v} |")
    print()
    print("| scenario | candidates | masked ok |")
    print("|---|---|---|")
    for sc, y in sorted(mask_yield.items()):
        print(f"| {sc} | {y['candidates']} | {y['masked_ok']} |")


if __name__ == "__main__":
    main(sys.argv[1:])
