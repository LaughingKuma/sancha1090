import importlib.util
from itertools import pairwise
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("estimator", REPO_ROOT / "livemap" / "estimator.py")
est = importlib.util.module_from_spec(spec)
spec.loader.exec_module(est)

TOKYO = (35.68, 139.77)
OSAKA = (34.69, 135.50)
SEATTLE = (47.45, -122.31)


def test_haversine_known_distance():
    assert est.haversine_nm(*TOKYO, *OSAKA) == pytest.approx(218, rel=0.03)


def test_haversine_symmetric_and_zero():
    assert est.haversine_nm(*TOKYO, *OSAKA) == pytest.approx(est.haversine_nm(*OSAKA, *TOKYO))
    assert est.haversine_nm(*TOKYO, *TOKYO) == 0.0


def test_initial_bearing_tokyo_osaka_is_wsw():
    assert est.initial_bearing_deg(*TOKYO, *OSAKA) == pytest.approx(255, abs=4)


def test_gc_point_endpoints_and_midpoint():
    assert est.gc_point(*TOKYO, *OSAKA, 0.0) == pytest.approx(TOKYO, abs=1e-9)
    assert est.gc_point(*TOKYO, *OSAKA, 1.0) == pytest.approx(OSAKA, abs=1e-9)
    mid = est.gc_point(*TOKYO, *OSAKA, 0.5)
    half = est.haversine_nm(*TOKYO, *OSAKA) / 2
    assert est.haversine_nm(*TOKYO, *mid) == pytest.approx(half, rel=1e-6)
    assert est.haversine_nm(*mid, *OSAKA) == pytest.approx(half, rel=1e-6)


def test_gc_point_antimeridian_no_linear_lon():
    # Tokyo->Seattle crosses 180; a linear lon average (~8.7 deg) would be wildly wrong
    mid = est.gc_point(*TOKYO, *SEATTLE, 0.5)
    assert abs(mid[1]) > 150
    # consecutive slerp points never jump across the map
    pts = [est.gc_point(*TOKYO, *SEATTLE, f / 20) for f in range(21)]
    for (_la1, lo1), (_la2, lo2) in pairwise(pts):
        dlon = abs(lo2 - lo1)
        assert min(dlon, 360 - dlon) < 30


def test_gc_point_near_antipodal_raises():
    anti = (-TOKYO[0], TOKYO[1] - 180.0)
    with pytest.raises(est.NearAntipodal):
        est.gc_point(*TOKYO, *anti, 0.5)


def test_dr_point_east_and_roundtrip():
    lat, lon = est.dr_point(*TOKYO, 90.0, 60.0)
    assert lat == pytest.approx(TOKYO[0], abs=0.35)
    assert lon > TOKYO[1] + 1.0
    assert est.haversine_nm(*TOKYO, lat, lon) == pytest.approx(60.0, rel=1e-3)


def test_dr_point_wraps_antimeridian():
    lat, lon = est.dr_point(35.0, 179.5, 90.0, 120.0)
    assert -180.0 <= lon <= 180.0
    assert lon < 0


def test_angle_diff():
    assert est.angle_diff_deg(350.0, 10.0) == pytest.approx(20.0)
    assert est.angle_diff_deg(90.0, 270.0) == pytest.approx(180.0)
    assert est.angle_diff_deg(45.0, 45.0) == 0.0
