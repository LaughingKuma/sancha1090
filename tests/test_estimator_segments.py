import importlib.util
from itertools import pairwise
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("estimator", REPO_ROOT / "livemap" / "estimator.py")
est = importlib.util.module_from_spec(spec)
spec.loader.exec_module(est)

CFG = est.DEFAULT_CONFIG


def fx(ts, lat=35.0, lon=139.0, alt=35000.0, ground=False, gs=450.0, track=90.0, src="adsblol"):
    return est.Fix(ts, lat, lon, alt, ground, gs, track, src)


def test_round_ts_deterministic():
    assert est.round_ts(10.5) == 11
    assert est.round_ts(11.5) == 12   # floor(x+0.5), never banker's
    assert est.round_ts(10.4) == 10


def test_build_gap_edges_exact_and_duration_honored():
    a, b = fx(1000.0, lon=139.0), fx(8200.0, lon=152.0, alt=37000.0)
    seg = est.build_gap(a, b, a, b, CFG)
    assert seg.kind == "gap"
    assert seg.points[0][:3] == [a.lon, a.lat, 1000]
    assert seg.points[-1][:3] == [b.lon, b.lat, 8200]
    ts = [p[2] for p in seg.points]
    assert ts == sorted(ts)
    # ~60 s cadence between interior points
    deltas = [t2 - t1 for t1, t2 in pairwise(ts)]
    assert max(deltas) <= 61 and min(deltas) >= 1


def test_build_gap_trapezoid_biases_position_toward_slow_end():
    # entry 300 kt, exit 500 kt: at mid-TIME the aircraft has covered <50% distance
    a, b = fx(0.0, lon=139.0, gs=300.0), fx(7200.0, lon=152.0, gs=500.0)
    seg = est.build_gap(a, b, a, b, CFG)
    mid = min(seg.points, key=lambda p: abs(p[2] - 3600))
    covered = est.haversine_nm(a.lat, a.lon, mid[1], mid[0])
    total = est.haversine_nm(a.lat, a.lon, b.lat, b.lon)
    assert covered / total < 0.48


def test_build_gap_altitude_linear_and_null_propagation():
    a, b = fx(0.0, lon=139.0, alt=30000.0), fx(7200.0, lon=152.0, alt=38000.0)
    seg = est.build_gap(a, b, a, b, CFG)
    mid = min(seg.points, key=lambda p: abs(p[2] - 3600))
    assert mid[3] == pytest.approx(34000.0, abs=800)
    a2 = fx(0.0, lon=139.0, alt=None)
    seg2 = est.build_gap(a2, b, a2, b, CFG)
    assert all(p[3] is None for p in seg2.points[1:-1])


def test_build_gap_bin():
    a, b = fx(0.0, lon=139.0), fx(2000.0, lon=142.0)
    assert est.build_gap(a, b, a, b, CFG).meta["bin"] == "gap_15_60m"
    a, b = fx(0.0, lon=139.0), fx(8000.0, lon=152.0)
    assert est.build_gap(a, b, a, b, CFG).meta["bin"] == "gap_60_180m"


def test_integrate_schedule_constant_speed():
    samples, capped = est.integrate_schedule([(0.0, 450.0), (450.0, 450.0)], 60.0, 999999.0)
    assert not capped
    assert samples[0] == (0.0, 0.0)
    x_end, t_end = samples[-1]
    assert x_end == pytest.approx(450.0, abs=0.5)
    assert t_end == pytest.approx(3600.0, rel=0.01)   # 450 nm at 450 kt = 1 h


def test_integrate_schedule_cap_clips_mid_ramp():
    # 450 kt hold for 60 nm then ramp to 140 over 40 nm; cap tight enough to land inside the ramp
    sched = [(0.0, 450.0), (60.0, 450.0), (100.0, 140.0)]
    full, _ = est.integrate_schedule(sched, 60.0, 999999.0)
    t_ramp_mid = (full[-1][1] + 60.0 / 450.0 * 3600.0) / 2
    samples, capped = est.integrate_schedule(sched, 60.0, t_ramp_mid)
    assert capped
    assert samples[-1][1] == pytest.approx(t_ramp_mid, abs=1.5)
    assert 60.0 < samples[-1][0] < 100.0   # cap landed inside the ramp; ramp portion retained


def test_build_dest_ext_stops_short_and_null_alt():
    dest = est.Endpoint(lat=35.0, lon=150.0, source="opensky_flights", agreement="majority")
    last = fx(10000.0, lon=139.0, gs=450.0, track=90.0)
    dist = est.haversine_nm(last.lat, last.lon, dest.lat, dest.lon)
    seg = est.build_dest_ext(last, last, dest, dist, CFG)
    assert seg.kind == "dest_ext"
    assert seg.points[0][:3] == [last.lon, last.lat, 10000]
    assert all(p[3] is None for p in seg.points)      # rev-10: ALL extension points alt-None
    end_dist = est.haversine_nm(dest.lat, dest.lon, seg.points[-1][1], seg.points[-1][0])
    assert end_dist == pytest.approx(CFG.dest_stop_short_nm, abs=1.0)
    assert seg.meta["capped"] is False
    assert seg.meta["confidence"]["endpoint_agreement"] == "majority"
    assert seg.meta["confidence"]["times_low_confidence"] is False


def test_build_dest_ext_exact_60s_cadence():
    dest = est.Endpoint(lat=35.0, lon=143.0)
    last = fx(0.0, lon=139.0, gs=450.0, track=90.0)
    dist = est.haversine_nm(last.lat, last.lon, dest.lat, dest.lon)
    seg = est.build_dest_ext(last, last, dest, dist, CFG)
    ts = [p[2] for p in seg.points]
    deltas = [t2 - t1 for t1, t2 in pairwise(ts[:-1])]   # exclude the exact off-grid terminal
    assert set(deltas) == {60}


def test_build_dest_ext_monotonic_below_target_entry():
    # entry 120 kt < 140 floor: min(entry, floor) target -> speed never increases -> duration
    # is at least distance/entry_speed
    dest = est.Endpoint(lat=35.0, lon=141.0)
    last = fx(0.0, lon=139.0, gs=120.0, track=90.0)
    dist = est.haversine_nm(last.lat, last.lon, dest.lat, dest.lon)
    seg = est.build_dest_ext(last, last, dest, dist, CFG)
    dur = seg.points[-1][2] - seg.points[0][2]
    assert dur >= (dist - CFG.dest_stop_short_nm) / 120.0 * 3600.0 * 0.99


def test_build_dest_ext_capped():
    dest = est.Endpoint(lat=35.0, lon=175.0)   # ~1750 nm at 450 kt ~ 3.9 h uncapped; slow it down
    last = fx(0.0, lon=139.0, gs=200.0, track=90.0)
    dist = est.haversine_nm(last.lat, last.lon, dest.lat, dest.lon)
    seg = est.build_dest_ext(last, last, dest, dist, CFG)
    assert seg.meta["capped"] is True
    assert seg.points[-1][2] - seg.points[0][2] == pytest.approx(CFG.dest_cap_s, abs=2)


def test_build_origin_ext_ascending_and_ends_at_first_fix():
    origin = est.Endpoint(lat=35.0, lon=130.0, source="swim", agreement="unanimous")
    first = fx(50000.0, lon=139.0, gs=450.0, track=90.0)
    dist = est.haversine_nm(origin.lat, origin.lon, first.lat, first.lon)
    seg = est.build_origin_ext(first, first, origin, dist, CFG)
    ts = [p[2] for p in seg.points]
    assert ts == sorted(ts)
    assert seg.points[-1][:3] == [first.lon, first.lat, 50000]
    assert seg.meta["confidence"]["times_low_confidence"] is True
    assert all(p[3] is None for p in seg.points)      # rev-10: ALL extension points alt-None
    # uncapped: reaches the airport
    start_dist = est.haversine_nm(origin.lat, origin.lon, seg.points[0][1], seg.points[0][0])
    assert start_dist < 2.0


def test_build_origin_ext_below_floor_monotonic():
    origin = est.Endpoint(lat=35.0, lon=137.0)
    first = fx(50000.0, lon=139.0, gs=120.0, track=90.0)
    dist = est.haversine_nm(origin.lat, origin.lon, first.lat, first.lon)
    seg = est.build_origin_ext(first, first, origin, dist, CFG)
    dur = seg.points[-1][2] - seg.points[0][2]
    # entry 120 < 160 floor: min() start -> constant profile, never accelerated
    assert dur >= dist / 120.0 * 3600.0 * 0.99


def test_build_origin_ext_cap_lands_mid_ramp():
    # 450 kt covers 1800 nm in the 4 h cap; total 1835 nm puts the clip ~35 nm from the
    # airport, INSIDE the 50 nm accel ramp: partial ramp must be retained (schedule-then-clip)
    olat, olon = est.dr_point(35.0, 139.0, 270.0, 1835.0)
    origin = est.Endpoint(lat=olat, lon=olon)
    first = fx(90000.0, lat=35.0, lon=139.0, gs=450.0, track=90.0)
    dist = est.haversine_nm(olat, olon, first.lat, first.lon)
    seg = est.build_origin_ext(first, first, origin, dist, CFG)
    assert seg.meta["capped"] is True
    start_dist = est.haversine_nm(olat, olon, seg.points[0][1], seg.points[0][0])
    assert 0.0 < start_dist < CFG.accel_ramp_nm


def test_build_origin_ext_capped_never_reaches_airport():
    origin = est.Endpoint(lat=35.0, lon=100.0)   # ~1900 nm; at 200 kt >> 4 h
    first = fx(90000.0, lon=139.0, gs=200.0, track=90.0)
    dist = est.haversine_nm(origin.lat, origin.lon, first.lat, first.lon)
    seg = est.build_origin_ext(first, first, origin, dist, CFG)
    assert seg.meta["capped"] is True
    start_dist = est.haversine_nm(origin.lat, origin.lon, seg.points[0][1], seg.points[0][0])
    assert start_dist > 100.0
    assert seg.points[-1][2] - seg.points[0][2] == pytest.approx(CFG.origin_cap_s, abs=2)


def test_build_dr_cap_and_altitude_held():
    anchor = fx(0.0, lon=139.0, alt=36000.0, gs=480.0, track=90.0)
    seg = est.build_dr(anchor, anchor, CFG)
    assert seg.kind == "dr"
    assert seg.points[0][:3] == [anchor.lon, anchor.lat, 0]
    assert seg.points[-1][2] == CFG.dr_cap_s
    assert all(p[3] == 36000.0 for p in seg.points)
    dist = est.haversine_nm(anchor.lat, anchor.lon, seg.points[-1][1], seg.points[-1][0])
    assert dist == pytest.approx(480.0 * CFG.dr_cap_s / 3600.0, rel=0.01)
    assert seg.meta["capped"] is True


def test_build_dr_none_altitude_held():
    anchor = fx(0.0, lon=139.0, alt=None, gs=480.0, track=180.0)
    seg = est.build_dr(anchor, anchor, CFG)
    assert all(p[3] is None for p in seg.points)
