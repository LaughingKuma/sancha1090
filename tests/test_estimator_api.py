import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("estimator", REPO_ROOT / "livemap" / "estimator.py")
est = importlib.util.module_from_spec(spec)
spec.loader.exec_module(est)

CFG = est.DEFAULT_CONFIG


def pt(ts, lat=35.0, lon=139.0, alt=35000.0, ground=False, gs=450.0, track=90.0, src="adsblol"):
    return (ts, lat, lon, alt, ground, gs, track, src)


def test_estimate_full_flight_gap_and_both_extensions():
    od = est.OD(origin=est.Endpoint(35.0, 130.0, "swim", "unanimous"),
                dest=est.Endpoint(35.0, 152.0, "opensky_flights", "majority"))
    points = [pt(0, lon=135.0), pt(300, lon=135.5), pt(4000, lon=141.0), pt(4300, lon=141.5)]
    r = est.estimate(points, od, CFG)
    kinds = [s.kind for s in r.segments]
    assert kinds == ["origin_ext", "gap", "dest_ext"]
    assert r.skips == []
    # wind request covers every segment index at least once
    assert {w[0] for w in r.wind_request} == {0, 1, 2}


def test_estimate_null_dest_routes_to_dr():
    od = est.OD(origin=est.Endpoint(35.0, 130.0))
    points = [pt(0, lon=135.0), pt(300, lon=135.5)]
    r = est.estimate(points, od, CFG)
    assert [s.kind for s in r.segments] == ["origin_ext", "dr"]


def test_estimate_bearing_rejected_dest_is_skip_only_no_dr():
    od = est.OD(dest=est.Endpoint(35.0, 152.0))
    points = [pt(0, lon=139.0, track=270.0), pt(300, lon=138.5, track=270.0)]
    r = est.estimate(points, od, CFG)
    assert all(s.kind != "dr" for s in r.segments)
    assert {"kind": "dest_ext", "reason": "bearing_conflict"} in r.skips


def test_estimate_missing_origin_is_skip():
    od = est.OD(dest=est.Endpoint(35.0, 152.0))
    points = [pt(0, lon=139.0), pt(300, lon=139.5)]
    r = est.estimate(points, od, CFG)
    assert {"kind": "origin_ext", "reason": "missing_endpoint"} in r.skips


def test_estimate_zero_fixes_no_input():
    r = est.estimate([], est.OD(), CFG)
    assert r.segments == [] and r.skips == [{"kind": "all", "reason": "no_input"}]


def test_estimate_single_fix_supports_dr_and_dest_ext():
    single = [pt(0, lon=139.0)]
    r = est.estimate(single, est.OD(), CFG)
    assert [s.kind for s in r.segments] == ["dr"]
    r2 = est.estimate(single, est.OD(dest=est.Endpoint(35.0, 152.0)), CFG)
    assert [s.kind for s in r2.segments] == ["dest_ext"]


def test_estimate_dr_motion_fallback_from_invalid_anchor():
    points = [pt(0, lon=139.0, gs=460.0, track=90.0), pt(300, lon=139.6, gs=None, track=None)]
    r = est.estimate(points, est.OD(), CFG)
    dr = [s for s in r.segments if s.kind == "dr"]
    assert dr and dr[0].meta["gs_entry_kt"] == 460.0
    assert dr[0].points[0][:2] == [139.6, 35.0]   # anchored at the LAST fix position


def test_estimate_near_antipodal_dest_is_skip():
    od = est.OD(dest=est.Endpoint(-35.0, -41.0))   # ~antipode of the fixes
    points = [pt(0, lon=139.0), pt(300, lon=139.5)]
    r = est.estimate(points, od, CFG)
    assert {"kind": "dest_ext", "reason": "near_antipodal"} in r.skips


def test_estimate_gap_skip_reason_surfaces():
    od = est.OD()
    points = [pt(0, lon=139.0, gs=None, track=None), pt(4000, lon=139.05, gs=None, track=None)]
    r = est.estimate(points, od, CFG)
    assert {"kind": "gap", "reason": "gap_kinematics"} in r.skips


def test_estimate_wind_request_spacing():
    od = est.OD(dest=est.Endpoint(35.0, 165.0))   # ~1280 nm ext -> ~6 samples at 250 nm
    points = [pt(0, lon=139.0), pt(300, lon=139.7)]
    r = est.estimate(points, od, CFG)
    dest_samples = [w for w in r.wind_request if r.segments[w[0]].kind == "dest_ext"]
    assert 4 <= len(dest_samples) <= 8


def test_estimate_on_ground_last_fix_skips_dr_as_on_ground():
    points = [pt(0, lon=139.0, gs=None, track=None), pt(60, lon=139.0, ground=True, gs=None, track=None)]
    r = est.estimate(points, est.OD(), CFG)
    assert [s.kind for s in r.segments] == []
    assert {"kind": "dr", "reason": "on_ground_edge"} in r.skips


def test_wind_request_interpolates_multiple_marks_in_one_leg():
    # regression: the pre-fix loop emitted duplicate endpoint samples when one leg crossed several marks
    seg = est.Segment("gap", [[139.0, 35.0, 0, None], [151.2, 35.0, 3600, None]], {})
    marks = est._wind_request_for(0, seg, CFG)
    assert len(marks) == 4
    leg = est.haversine_nm(35.0, 139.0, 35.0, 151.2)
    for mark, dist in zip(marks[2:], (125.0, 375.0), strict=True):
        lat, lon = est.gc_point(35.0, 139.0, 35.0, 151.2, dist / leg)
        assert mark[1] == pytest.approx(lat)
        assert mark[2] == pytest.approx(lon)
        assert mark[1] > 35.0   # same-latitude great circle arcs poleward, pinning (lat, lon) slot order
        assert mark[4] == est.round_ts(3600 * dist / leg)


def test_wind_request_anchors_and_never_invented_altitude():
    # regression: interpolated marks once inherited a real edge altitude at positions the gap
    # deliberately serves as NULL; anchors carry the design's TAS-inference points (both gap edges)
    seg = est.Segment("gap", [[139.0, 35.0, 0, None], [151.2, 35.0, 3600, 35000.0]], {})
    marks = est._wind_request_for(0, seg, CFG)
    assert marks[0][1:] == (35.0, 139.0, None, 0)
    assert marks[1][1:] == (35.0, 151.2, 35000.0, 3600)
    assert all(m[3] is None for m in marks[2:])
    dr = est.Segment("dr", [[139.0, 35.0, 0, 36000.0], [139.5, 35.0, 600, 36000.0]], {})
    dr_marks = est._wind_request_for(1, dr, CFG)
    assert dr_marks[0][1:] == (35.0, 139.0, 36000.0, 0)
