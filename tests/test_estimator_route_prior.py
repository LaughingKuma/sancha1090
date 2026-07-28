import importlib.util
from itertools import pairwise
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("estimator", REPO_ROOT / "livemap" / "estimator.py")
est = importlib.util.module_from_spec(spec)
spec.loader.exec_module(est)

CFG = est.DEFAULT_CONFIG

# measured filed plans (bronze.swim_flightdata, 2026-07-28)
DAL69_WPS = [(48.0, -140.0), (46.0, -150.0), (44.0, -160.0), (43.0, -170.0),
             (42.0, -180.0), (41.0, 170.0), (40.0, 160.0)]
JAL41_WPS = [(81.0, -120.0), (82.0, -100.0), (82.0, -80.0), (82.0, -60.0),
             (80.0, -40.0), (74.0, -20.0), (66.0, -10.0)]


def fx(ts, lat=35.0, lon=139.0, alt=35000.0, ground=False, gs=450.0, track=90.0, src="adsblol"):
    return est.Fix(ts, lat, lon, alt, ground, gs, track, src)


def pt(ts, lat=35.0, lon=139.0, alt=35000.0, ground=False, gs=450.0, track=90.0, src="adsblol"):
    return (ts, lat, lon, alt, ground, gs, track, src)


def test_select_drops_out_of_lens_keeps_in_lens():
    a, b = fx(0.0, 35.0, 140.0), fx(7200.0, 35.0, 160.0)
    got = est.select_route_prior(a, b, [(36.0, 150.0), (35.0, 120.0)], CFG)
    assert got == [(36.0, 150.0)]   # behind the entry fix -> dropped, not fatal


def test_select_monotonicity_violation_is_fatal():
    a, b = fx(0.0, 35.0, 140.0), fx(7200.0, 35.0, 160.0)
    assert est.select_route_prior(a, b, [(36.0, 155.0), (36.0, 145.0)], CFG) is None


def test_select_detour_cap_rejects():
    a, b = fx(0.0, 35.0, 140.0), fx(7200.0, 35.0, 160.0)
    assert est.select_route_prior(a, b, [(45.0, 150.0)], CFG) is None      # ratio 1.54
    assert est.select_route_prior(a, b, [(42.0, 150.0)], CFG) is not None  # ratio 1.28


def test_select_all_dropped_or_empty_is_none():
    a, b = fx(0.0, 35.0, 140.0), fx(7200.0, 35.0, 160.0)
    assert est.select_route_prior(a, b, [(35.0, 100.0)], CFG) is None
    assert est.select_route_prior(a, b, [], CFG) is None
    assert est.select_route_prior(a, b, None, CFG) is None


def test_build_gap_without_prior_is_unchanged():
    a, b = fx(1000.0, lon=139.0), fx(8200.0, lon=152.0, alt=37000.0)
    base, explicit = est.build_gap(a, b, a, b, CFG), est.build_gap(a, b, a, b, CFG, None)
    assert explicit.points == base.points
    assert explicit.meta == base.meta
    assert "route" not in base.meta and base.meta["bin"] == "gap_60_180m"


def test_route_prior_bin_and_meta():
    a, b = fx(0.0, 35.0, 140.0), fx(7200.0, 35.0, 160.0)
    seg = est.build_gap(a, b, a, b, CFG, [(42.0, 150.0)])
    assert seg.meta["bin"] == "gap_60_180m_route"
    assert seg.meta["route"] == {"prior": True, "tokens": 1}


def test_dal69_chain_crosses_antimeridian():
    a = fx(0.0, 48.8, -127.1)
    b = fx(31360.0, 36.9, 144.8)
    prior = est.select_route_prior(a, b, DAL69_WPS, CFG)
    assert len(prior) == 7
    seg = est.build_gap(a, b, a, b, CFG, prior)
    lons = [p[0] for p in seg.points]
    # wrap-normalized steps stay tiny: the path crosses +-180, it never sweeps the long way
    assert max(abs(((l2 - l1 + 180.0) % 360.0) - 180.0) for l1, l2 in pairwise(lons)) < 1.0
    crossing = [p for p in seg.points if abs(abs(p[0]) - 180.0) < 1.0]
    assert crossing and all(41.5 <= p[1] <= 43.0 for p in crossing)
    assert min(lons) < -179.0 and max(lons) > 179.0


def test_jal41_polar_chain_follows_filed_latitudes():
    a = fx(0.0, 69.1, -163.7)
    b = fx(21976.0, 64.4, -8.5)
    prior = est.select_route_prior(a, b, JAL41_WPS, CFG)
    assert len(prior) == 7
    seg = est.build_gap(a, b, a, b, CFG, prior)
    max_lat = max(p[1] for p in seg.points)
    plain_max_lat = max(p[1] for p in est.build_gap(a, b, a, b, CFG).points)
    assert max_lat >= 81.0
    # the filed 82N track is SOUTH of the pure GC arc: the prior must not be mistaken for it
    assert max_lat < plain_max_lat - 2.0


def test_route_prior_duration_and_timestamps():
    a, b = fx(1000.0, 35.0, 140.0), fx(8200.0, 35.0, 160.0)
    seg = est.build_gap(a, b, a, b, CFG, [(42.0, 150.0)])
    ts = [p[2] for p in seg.points]
    assert seg.points[0][:2] == [a.lon, a.lat] and seg.points[-1][:2] == [b.lon, b.lat]
    assert ts[0] == 1000 and ts[-1] == 8200
    assert ts[-1] - ts[0] == b.ts - a.ts
    assert all(isinstance(t, int) for t in ts)
    assert all(t2 > t1 for t1, t2 in pairwise(ts))
    deltas = [t2 - t1 for t1, t2 in pairwise(ts)]
    assert max(deltas) <= 61


def test_route_prior_altitude_rules_unchanged():
    a, b = fx(0.0, 35.0, 140.0, alt=30000.0), fx(7200.0, 35.0, 160.0, alt=38000.0)
    seg = est.build_gap(a, b, a, b, CFG, [(42.0, 150.0)])
    mid = min(seg.points, key=lambda p: abs(p[2] - 3600))
    assert mid[3] == pytest.approx(34000.0, abs=800)
    a2 = fx(0.0, 35.0, 140.0, alt=None)
    seg2 = est.build_gap(a2, b, a2, b, CFG, [(42.0, 150.0)])
    assert all(p[3] is None for p in seg2.points[1:-1])


def test_infeasible_chain_leg_falls_back_to_plain_gc():
    a, b = fx(0.0, 0.0, 0.0), fx(7200.0, 0.0, 10.0)
    # consecutive waypoints exactly antipodal: the whole prior drops, the gap is still bridged
    seg = est.build_gap(a, b, a, b, CFG, [(0.0, -179.0), (0.0, 1.0)])
    assert seg.meta["bin"] == "gap_60_180m" and "route" not in seg.meta
    assert seg.points == est.build_gap(a, b, a, b, CFG).points


def test_estimate_route_pts_none_is_identical():
    points = [pt(0, lon=139.0), pt(7200, lon=152.0)]
    r0 = est.estimate(points, est.OD(), CFG)
    r1 = est.estimate(points, est.OD(), CFG, None)
    assert [s.points for s in r0.segments] == [s.points for s in r1.segments]
    assert [s.meta for s in r0.segments] == [s.meta for s in r1.segments]
    assert r0.skips == r1.skips and r0.wind_request == r1.wind_request


def test_estimate_applies_prior_to_eligible_gap():
    points = [pt(0, 35.0, 140.0), pt(7200, 35.0, 160.0)]
    r = est.estimate(points, est.OD(), CFG, route_pts=[(36.0, 150.0)])
    gap = next(s for s in r.segments if s.kind == "gap")
    assert gap.meta["bin"] == "gap_60_180m_route"
    assert gap.meta["route"]["tokens"] == 1
    # a prior that fails a guard leaves the gap exactly as the pure-GC bridge
    r2 = est.estimate(points, est.OD(), CFG, route_pts=[(45.0, 150.0)])
    gap2 = next(s for s in r2.segments if s.kind == "gap")
    assert gap2.meta["bin"] == "gap_60_180m" and "route" not in gap2.meta


def test_wind_request_follows_the_chain_not_the_direct_gc():
    a, b = fx(0.0, 35.0, 140.0), fx(7200.0, 35.0, 160.0)
    seg = est.build_gap(a, b, a, b, CFG, [(42.0, 150.0)])
    marks = est._wind_request_for(0, seg, CFG)
    interior = marks[2:]
    assert interior
    for _idx, lat, lon, _alt, _ts in interior:
        on_chain = min(est.haversine_nm(lat, lon, *est.gc_point(p[0], p[1], q[0], q[1], f / 400.0))
                       for p, q in pairwise([(35.0, 140.0), (42.0, 150.0), (35.0, 160.0)])
                       for f in range(401))
        off_direct = min(est.haversine_nm(lat, lon, *est.gc_point(35.0, 140.0, 35.0, 160.0, f / 400.0))
                         for f in range(401))
        assert on_chain < 2.0 < off_direct


def test_select_rejects_chain_speed_above_envelope():
    # r2: eligibility passed 798 kt on the DIRECT line; the chained detour implied ~1,178 kt —
    # the prior must not smuggle effective speed past gap_max_kt
    a, b = fx(0.0, 0.0, 0.0), fx(2706.0, 0.0, 10.0)
    assert est.select_route_prior(a, b, [(5.5, 5.0)], CFG) is None
    assert est.select_route_prior(a, b, [(0.2, 5.0)], CFG) is not None
