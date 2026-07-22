import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("estimator", REPO_ROOT / "livemap" / "estimator.py")
est = importlib.util.module_from_spec(spec)
spec.loader.exec_module(est)

CFG = est.DEFAULT_CONFIG


def fx(ts, lat=35.0, lon=139.0, alt=35000.0, ground=False, gs=450.0, track=90.0, src="adsblol"):
    return est.Fix(ts, lat, lon, alt, ground, gs, track, src)


def test_norm_track():
    assert est.norm_track(370.0) == 10.0
    assert est.norm_track(-10.0) == 350.0
    assert est.norm_track(None) is None
    assert est.norm_track(float("nan")) is None
    assert est.norm_track(float("inf")) is None


def test_valid_motion_per_kind_gap_ignores_track():
    f = fx(0, track=None)
    assert est.valid_motion(f, "gap", CFG) is True
    assert est.valid_motion(f, "ext", CFG) is False
    assert est.valid_motion(f, "dr", CFG) is False


def test_valid_motion_envelope():
    assert est.valid_motion(fx(0, gs=0.0), "gap", CFG) is False
    assert est.valid_motion(fx(0, gs=900.0), "gap", CFG) is False
    assert est.valid_motion(fx(0, gs=float("nan")), "gap", CFG) is False
    assert est.valid_motion(fx(0, gs=None), "gap", CFG) is False
    assert est.valid_motion(fx(0, gs=450.0), "gap", CFG) is True


def test_find_motion_directional_and_bounded():
    fixes = [fx(0, gs=None), fx(100, gs=None), fx(200, gs=440.0), fx(900, gs=430.0)]
    # backward search from idx 3 reaches idx 2 (700 s back <= 600? no: 900-200=700 > 600) -> only idx 3 itself
    got = est.find_motion(fixes, 3, -1, "gap", CFG, stop_idx=-1)
    assert got is fixes[3]
    # backward from idx 1: idx1 invalid, idx0 invalid -> None
    assert est.find_motion([fx(0, gs=None), fx(100, gs=None)], 1, -1, "gap", CFG, stop_idx=-1) is None
    # forward from idx 0 finds idx 2 within 600 s
    assert est.find_motion(fixes, 0, +1, "gap", CFG, stop_idx=4) is fixes[2]


def test_find_motion_never_crosses_stop_idx():
    fixes = [fx(0, gs=None), fx(50, gs=440.0)]
    # stop_idx=1 excludes index 1 even though it is in time range
    assert est.find_motion(fixes, 0, +1, "gap", CFG, stop_idx=1) is None


def test_prepare_sorts_dedups_drops():
    raw = [
        (200.0, 35.0, 139.0, None, False, 400.0, 90.0, "a"),
        (100.0, 35.0, 139.0, 100.5, False, 400.0, 90.0, "a"),
        (100.0, 36.0, 139.0, None, False, 400.0, 90.0, "b"),   # dup ts: dropped
        (300.0, None, 139.0, None, False, 400.0, 90.0, "a"),    # no lat: dropped
        (400.0, 35.0, float("nan"), None, False, 400.0, 90.0, "a"),  # nan lon: dropped
    ]
    out = est.prepare(raw)
    assert [f.ts for f in out] == [100.0, 200.0]
    assert out[0].alt_ft == 100.5


def test_detect_gaps():
    fixes = [fx(0), fx(300), fx(1200), fx(1300), fx(3000)]
    assert est.detect_gaps(fixes, CFG) == [(1, 2), (3, 4)]


def test_gap_eligibility_happy_and_null_track_ok():
    fixes = [fx(0, lon=139.0, track=None), fx(4000, lon=145.0, track=None)]
    got = est.gap_eligibility(fixes, 0, 1, [(0, 1)], CFG)
    assert got == (fixes[0], fixes[1])


def test_gap_eligibility_kinematic_guard():
    # ~9 nm apart over 4000 s -> ~8 kt implied: absurd slow
    slow = [fx(0, lon=139.0), fx(4000, lon=139.2)]
    assert est.gap_eligibility(slow, 0, 1, [(0, 1)], CFG) == "gap_kinematics"
    # ~1500 nm over 4000 s -> ~1350 kt implied: impossible
    fast = [fx(0, lon=110.0), fx(4000, lon=140.0)]
    assert est.gap_eligibility(fast, 0, 1, [(0, 1)], CFG) == "gap_kinematics"


def test_gap_eligibility_on_ground_and_motion_fallback_not_across_gap():
    grounded = [fx(0, ground=True), fx(4000, lon=145.0)]
    assert est.gap_eligibility(grounded, 0, 1, [(0, 1)], CFG) == "on_ground_edge"
    # entry edge has no gs; the only valid-gs fix is across the PREVIOUS gap -> invalid_motion
    fixes = [fx(0, gs=440.0), fx(5000, lon=141.0, gs=None), fx(9000, lon=145.0, gs=430.0)]
    gaps = [(0, 1), (1, 2)]
    assert est.gap_eligibility(fixes, 1, 2, gaps, CFG) == "invalid_motion"


def test_ext_eligibility_dest_happy():
    od = est.OD(dest=est.Endpoint(lat=35.0, lon=150.0, source="opensky_flights", agreement="majority"))
    fixes = [fx(0, lon=139.0, track=90.0, gs=450.0)]
    got = est.ext_eligibility(fixes, od, "dest", CFG)
    assert isinstance(got, dict)
    assert got["motion"] is fixes[0]
    assert got["dist_nm"] > 400
    assert got["bearing_deg"] == __import__("pytest").approx(90.0, abs=4)


def test_ext_eligibility_bearing_conflict_hard_stop():
    od = est.OD(dest=est.Endpoint(lat=35.0, lon=150.0))
    fixes = [fx(0, lon=139.0, track=270.0)]  # flying due west, dest due east
    assert est.ext_eligibility(fixes, od, "dest", CFG) == "bearing_conflict"


def test_ext_eligibility_below_min_distance():
    od = est.OD(dest=est.Endpoint(lat=35.0, lon=139.4))  # ~20 nm ~ 37 km < 50 km
    fixes = [fx(0, lon=139.0, track=90.0)]
    assert est.ext_eligibility(fixes, od, "dest", CFG) == "below_min_distance"


def test_ext_eligibility_entry_speed_envelope():
    od = est.OD(dest=est.Endpoint(lat=35.0, lon=150.0))
    fixes = [fx(0, lon=139.0, gs=750.0, track=90.0)]
    assert est.ext_eligibility(fixes, od, "dest", CFG) == "entry_speed_envelope"


def test_ext_eligibility_origin_bearing_uses_origin_to_first_fix():
    # origin west of first fix; first-fix track eastbound 90 -> bearing(origin->first) ~90 -> eligible
    od = est.OD(origin=est.Endpoint(lat=35.0, lon=130.0))
    fixes = [fx(0, lon=139.0, track=90.0)]
    got = est.ext_eligibility(fixes, od, "origin", CFG)
    assert isinstance(got, dict)
    # origin missing -> skip (never DR for origins)
    assert est.ext_eligibility(fixes, est.OD(), "origin", CFG) == "missing_endpoint"
