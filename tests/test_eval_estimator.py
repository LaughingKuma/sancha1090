import importlib.util
from itertools import pairwise
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("eval_estimator", REPO_ROOT / "scripts" / "eval_estimator.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def mk(n=100, t0=0, dt=60, lon0=130.0, dlon=0.05):
    # gs matches the geometry: 0.05 deg lon / 60 s at lat 35 ~ 147.3 kt (keeps DR scoring honest)
    return [(t0 + i * dt, 35.0, lon0 + i * dlon, 35000.0, False, 147.3, 90.0, "adsblol")
            for i in range(n)]


def test_mask_terminal():
    kept, masked = ev.mask_terminal(mk(), 600)
    assert len(masked) == 10 and len(kept) == 90
    assert kept[-1][0] < masked[0][0]


def test_mask_leading():
    kept, masked = ev.mask_leading(mk(), 600)
    assert len(masked) == 10 and kept[0][0] > masked[-1][0]


def test_mask_window():
    kept, masked = ev.mask_window(mk(), 0.5, 1200)
    assert len(masked) == 20
    ts = [p[0] for p in kept]
    assert max(t2 - t1 for t1, t2 in pairwise(ts)) > 1200


def test_mask_lonbox():
    kept, masked = ev.mask_lonbox(mk(), 131.0, 132.0)
    assert all(131.0 <= p[2] <= 132.0 for p in masked)
    assert all(not (131.0 <= p[2] <= 132.0) for p in kept)


FLIGHT_ROW = {"flight_id": 1, "origin_lat": 35.0, "origin_lon": 129.95, "origin_source": "swim",
              "origin_agreement": "unanimous", "dest_lat": 35.0, "dest_lon": 136.0,
              "dest_source": "opensky_flights", "dest_agreement": "majority"}


def test_pctl_nearest_rank():
    assert ev.pctl([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert ev.pctl([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9
    assert ev.pctl([7], 90) == 7


def test_window_scenario_emits_gap_row_with_bin():
    rows = ev.evaluate_flight(FLIGHT_ROW, mk(n=240), "window")
    gap_rows = [r for r in rows if r["target_kind"] == "gap"]
    assert gap_rows and gap_rows[0]["eligible"]
    assert gap_rows[0]["bin"] in {"gap_15_60m", "gap_60_180m", "gap_180m_plus"}
    assert gap_rows[0]["errors"] and max(gap_rows[0]["errors"]) < 15.0
    assert gap_rows[0]["source"] is None    # gaps carry no endpoint identity


def test_terminal_scenario_targets_dest_ext_with_relevant_endpoint():
    rows = ev.evaluate_flight(FLIGHT_ROW, mk(n=120), "terminal")
    assert rows and all(r["target_kind"] == "dest_ext" for r in rows)
    assert rows[0]["agreement"] == "majority" and rows[0]["source"] == "opensky_flights"


def test_dr_scenario_hides_destination_and_scores_horizon():
    rows = ev.evaluate_flight(FLIGHT_ROW, mk(n=120), "dr")
    assert rows and all(r["target_kind"] == "dr" for r in rows)
    assert any(r["eligible"] for r in rows)
    assert max(rows[0]["errors"]) < 20.0    # gs matches geometry: DR nails a straight line


def test_short_flight_unfit_mask_returns_empty():
    assert ev.evaluate_flight(FLIGHT_ROW, mk(n=30), "window") == []


def test_summarize_pools_point_errors():
    rows = [{"scenario": "window", "target_kind": "gap", "bin": "gap_15_60m", "source": None,
             "agreement": None, "region": "other", "eligible": True, "errors": [1.0, 2.0, 100.0],
             "eta_s": None, "skip_reason": None},
            {"scenario": "window", "target_kind": "gap", "bin": "gap_15_60m", "source": None,
             "agreement": None, "region": "other", "eligible": True, "errors": [3.0], "eta_s": None,
             "skip_reason": None}]
    s = ev.summarize(rows)
    key = "window|gap|gap_15_60m|None|None|other"
    assert s[key]["n"] == 2
    assert s[key]["pos_p90_km"] == 100.0    # point-level pool, NOT a p90-of-p50s


def test_lonbox_evaluates_full_crossing_as_one_experiment():
    # truth on both sides of the box: exactly one gap experiment, binned by its real duration
    rows = ev.evaluate_flight(FLIGHT_ROW, mk(n=700, lon0=130.0, dlon=-0.05), "lonbox")
    assert len(rows) == 1
    assert rows[0]["eligible"]
    assert rows[0]["bin"] == "gap_180m_plus"
    assert rows[0]["region"] == "china"


def test_lonbox_boundary_skim_rejected_at_its_own_duration_bin():
    # a skim re-enters near its exit point, so the implied bridge speed is absurd-slow: the
    # kinematic guard must reject, and the row must carry the run's true duration bin —
    # regression for whole-mask spans mislabeling short skims as gap_180m_plus
    west = [(i * 60, 35.0, 130.0 - 0.05 * i, 35000.0, False, 147.3, 270.0, "a") for i in range(161)]
    east = [((161 + i) * 60, 35.0, 122.05 + 0.05 * i, 35000.0, False, 147.3, 90.0, "a") for i in range(160)]
    rows = ev.evaluate_flight(FLIGHT_ROW, west + east, "lonbox")
    assert len(rows) == 1
    assert not rows[0]["eligible"]
    assert rows[0]["skip_reason"] == "gap_kinematics"
    assert rows[0]["bin"] == "gap_60_180m"


def test_lonbox_edge_only_mask_is_not_an_experiment():
    # an edge visit tests truncation, not gap bridging: it must stay out of the denominator
    assert ev.evaluate_flight(FLIGHT_ROW, mk(n=240, lon0=120.0), "lonbox") == []
    assert ev.evaluate_flight(FLIGHT_ROW, mk(n=240), "lonbox") == []


def test_skip_table_counts_target_kind_reasons():
    rows = [{"scenario": "terminal", "target_kind": "dest_ext", "bin": None, "source": None,
             "agreement": None, "eligible": False, "errors": [], "eta_s": None,
             "skip_reason": "bearing_conflict"}]
    assert ev.skip_table(rows) == {("dest_ext", "bearing_conflict"): 1}


def test_lonbox_two_bounded_visits_are_independent_experiments():
    # regression: pre-round-6 the whole mask was one experiment, so a flight's second
    # visit vanished behind the first — two bounded dips must yield two rows
    lons = ([125.3, 125.2, 125.1]
            + [124.9 - 0.05 * i for i in range(12)]
            + [125.1, 125.2, 125.1]
            + [124.9 - 0.05 * i for i in range(12)]
            + [125.1, 125.2, 125.3])
    pts = [(i * 60, 35.0, lon, 35000.0, False, 147.3, 90.0, "a") for i, lon in enumerate(lons)]
    rows = ev.evaluate_flight(FLIGHT_ROW, pts, "lonbox")
    assert len(rows) == 2
    assert all(r["skip_reason"] == "gap_kinematics" and r["bin"] == "gap_15_60m" for r in rows)
