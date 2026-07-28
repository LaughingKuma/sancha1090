import datetime
import importlib.util
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def livemap():
    spec = importlib.util.spec_from_file_location("livemap_app_route", REPO_ROOT / "livemap" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AUTH = ("abc123", "DAL69", False, 1765500000, 1765507200, datetime.date(2026, 6, 1))
# one 2 h hole between two airborne fixes: the only shape a filed route may shape (design 6a)
GAP_POINTS = [
    (1765500000, 35.0, 140.0, 35000, 0, 450, 90, "adsblol"),
    (1765507200, 35.0, 160.0, 35000, 0, 450, 90, "adsblol"),
]
TIGHT_POINTS = [
    (1765500000, 35.0, 140.0, 35000, 0, 450, 90, "adsblol"),
    (1765500060, 35.0, 140.1, 35000, 0, 450, 90, "adsblol"),
]
FLIGHT = ("DAL69", 1765500000, 1765507200, "KSEA", "RCTP")
# 4200N/15000E sits inside the lens and 1.28x the direct GC — it survives every estimator guard
ROUTE_STR = "RJTT..SEFIX..4200N/15000E..EMRON.OTR7.ADNAP..KSEA"
ROUTE_STR_ALT = "RJTT..SEFIX..4100N/14500E..4200N/15000E..EMRON..KSEA"
PLAN_TS = 1765499000


@pytest.fixture(autouse=True)
def allow_path_auth(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(livemap, "_fetch_path_auth", lambda _fid: AUTH)
    monkeypatch.setattr(livemap, "_path_head",
                        {"expiry": float("inf"), "head": datetime.date(2100, 1, 1)})
    monkeypatch.setattr(livemap, "_est_cache", {})


def _loader_result(status="settled", points=GAP_POINTS, auth=AUTH, as_of=1765507200):
    return {"status": status, "points": points, "auth": auth, "as_of": as_of}


def _stub_serving(livemap, monkeypatch, result=None, route=(ROUTE_STR, PLAN_TS)):
    async def load(_flight_id):
        return result or _loader_result()

    calls = []

    def fetch_route(callsign, start_time, end_time, origin_icao, dest_icao):
        calls.append((callsign, start_time, end_time, origin_icao, dest_icao))
        return route

    monkeypatch.setattr(livemap, "_load_path_input", load)
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: (livemap.est.OD(), FLIGHT))
    monkeypatch.setattr(livemap, "_fetch_route", fetch_route)
    return calls


def _gap_meta(payload):
    return next(s["meta"] for s in payload["segments"] if s["kind"] == "gap")


def test_settled_arm_serves_route_prior_bin_and_provenance(livemap, monkeypatch):
    calls = _stub_serving(livemap, monkeypatch)
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    response = TestClient(livemap.app).get("/path/42/estimate")
    payload = response.json()

    assert response.status_code == 200
    assert calls == [FLIGHT]
    meta = _gap_meta(payload)
    assert meta["uncertainty"]["bin"] == "gap_60_180m_route"
    assert meta["route"] == {"prior": True, "tokens": 1, "plan_ts": PLAN_TS}
    # the log is a lossless record of the wire: same bin, same meta, byte-identical json
    idx = livemap.ess.INSERT_COLUMNS.index
    gap_row = next(r for r in enqueued[0] if r[idx("uncertainty_bin")] == "gap_60_180m_route")
    assert json.loads(gap_row[idx("meta_json")])["route"] == meta["route"]
    assert gap_row[idx("uncertainty_p50_km")] == meta["uncertainty"]["p50_km"]
    assert len(livemap.ess.INSERT_COLUMNS) == 31 and len(livemap.ess.INSERT_TYPES) == 31


def test_provisional_arm_serves_route_prior(livemap, monkeypatch):
    _stub_serving(livemap, monkeypatch, result=_loader_result(status="provisional"))
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)

    payload = TestClient(livemap.app).get("/path/42/estimate").json()

    assert payload["input_provisional"] is True
    assert _gap_meta(payload)["route"]["plan_ts"] == PLAN_TS


def test_no_plan_serves_exactly_the_pure_gc_bridge(livemap, monkeypatch):
    _stub_serving(livemap, monkeypatch, route=None)
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)
    client = TestClient(livemap.app)

    missing = client.get("/path/42/estimate")

    # a flight the reconciled mart has no callsign/window for takes the same pure-GC path
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: (livemap.est.OD(), None))
    monkeypatch.setattr(livemap, "_est_cache", {})
    baseline = client.get("/path/42/estimate")

    assert missing.content == baseline.content
    assert _gap_meta(missing.json())["uncertainty"]["bin"] == "gap_60_180m"
    assert "route" not in _gap_meta(missing.json())


def test_route_fetch_failure_and_timeout_serve_the_pure_gc_bridge(livemap, monkeypatch):
    # the REAL _fetch_route runs here: its own guard is what must absorb a dead or slow warehouse
    async def load(_flight_id):
        return _loader_result()

    monkeypatch.setattr(livemap, "_load_path_input", load)
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: (livemap.est.OD(), FLIGHT))
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)
    client = TestClient(livemap.app)

    def down():
        raise ConnectionError("clickhouse unavailable")

    monkeypatch.setattr(livemap, "_ch_client", down)
    failed = client.get("/path/42/estimate")

    class _TimingOut:
        def query(self, *_a, **_k):
            raise TimeoutError("max_execution_time exceeded")

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: _TimingOut())
    monkeypatch.setattr(livemap, "_est_cache", {})
    timed_out = client.get("/path/42/estimate")

    monkeypatch.setattr(livemap, "_fetch_route", lambda *_a: None)
    monkeypatch.setattr(livemap, "_est_cache", {})
    absent = client.get("/path/42/estimate")

    assert failed.status_code == 200
    assert failed.content == absent.content == timed_out.content
    assert _gap_meta(absent.json())["uncertainty"]["bin"] == "gap_60_180m"


def test_fetch_route_returns_none_on_empty_or_blank_rows(livemap, monkeypatch):
    class _Client:
        def __init__(self, rows):
            self.rows = rows

        def query(self, *_a, **_k):
            return type("_Res", (), {"result_rows": self.rows})()

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: _Client([]))
    assert livemap._fetch_route("DAL69", 1765500000, 1765507200, "KSEA", "RCTP") is None
    monkeypatch.setattr(livemap, "_ch_client", lambda: _Client([(None, PLAN_TS)]))
    assert livemap._fetch_route("DAL69", 1765500000, 1765507200, "KSEA", "RCTP") is None
    monkeypatch.setattr(livemap, "_ch_client", lambda: _Client([(ROUTE_STR, PLAN_TS)]))
    assert livemap._fetch_route("dal69 ", 1765500000, 1765507200, "KSEA", "RCTP") == (ROUTE_STR, PLAN_TS)


def test_fetch_route_binds_parameters_and_bounds_execution(livemap, monkeypatch):
    seen = {}

    class _Client:
        def query(self, sql, parameters=None, settings=None):
            seen.update(sql=sql, parameters=parameters, settings=settings)
            return type("_Res", (), {"result_rows": [(ROUTE_STR, PLAN_TS)]})()

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: _Client())
    livemap._fetch_route(" dal69 ", 1765500000, 1765507200, "ksea", "rctp")

    assert seen["parameters"] == {"callsign": "DAL69", "start": 1765500000, "end": 1765507200,
                                  "origin": "KSEA", "dest": "RCTP"}
    assert seen["settings"] == {"max_execution_time": livemap.EST_ROUTE_TIMEOUT_S}
    assert "DAL69" not in seen["sql"]   # bound, never interpolated
    assert "{callsign:String}" in seen["sql"]


def test_gap_free_input_never_pays_the_swim_read(livemap, monkeypatch):
    calls = _stub_serving(livemap, monkeypatch, result=_loader_result(points=TIGHT_POINTS))
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)

    response = TestClient(livemap.app).get("/path/42/estimate")

    assert response.status_code == 200
    assert calls == []


def test_settled_empty_and_live_arms_never_fetch_a_route(livemap, monkeypatch):
    def never(*_args, **_kwargs):
        pytest.fail("route prior is gap-bridge only")

    monkeypatch.setattr(livemap, "_fetch_route", never)
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)

    async def load(_flight_id):
        return _loader_result(status="settled_empty", points=[])

    monkeypatch.setattr(livemap, "_load_path_input", load)
    client = TestClient(livemap.app)
    settled_empty = client.get("/path/42/estimate")

    now = livemap.time.time()
    monkeypatch.setattr(livemap, "_snapshot", {"server_ts": now, "aircraft": [
        {"hex": "abc123", "flight": "ANA1", "lat": 35.0, "lon": 139.5, "alt_baro": "35000",
         "gs": 450.0, "track": 90.0, "capture_ts": now - 5.0},
    ]})
    live = client.get("/estimate/live/abc123")

    assert settled_empty.status_code == 200 and live.status_code == 200
    assert [s["kind"] for s in live.json()["segments"]] == ["dr"]


def test_every_bin_the_estimator_can_emit_has_a_band(livemap):
    src = (REPO_ROOT / "livemap" / "estimator.py").read_text()
    # tripwire: a new bin site in the estimator must be enumerated here before it can serve
    assert src.count('"bin":') == 4
    literal = set(re.findall(r'"bin": "([a-z0-9_]+)"', src))
    assert literal == {"dest_ext", "origin_ext", "dr"}
    gap_bins = {livemap.est._gap_bin(d) for d in (1, 121, 601, 900, 3601, 10801, 86400)}
    assert len(gap_bins) == 4
    emitted = literal | gap_bins | {b + "_route" for b in gap_bins}

    for bin_name in sorted(emitted):
        band = livemap.ess.UNCERTAINTY_BANDS[bin_name]
        assert band["p50_km"] > 0 and band["p90_km"] > 0
    # _route bins carry base band VALUES but always as floors (adversarial r6): the bands were
    # calibrated on GC bridges and an admitted prior can sit far off that GC
    for base in gap_bins:
        rb = livemap.ess.UNCERTAINTY_BANDS[base + "_route"]
        bb = livemap.ess.UNCERTAINTY_BANDS[base]
        assert (rb["p50_km"], rb["p90_km"]) == (bb["p50_km"], bb["p90_km"])
        assert rb.get("floor") is True


def test_fingerprint_separates_absent_empty_and_distinct_priors(livemap):
    fp = livemap.ess.input_fingerprint
    od = livemap.est.OD()
    absent = fp(GAP_POINTS, od)
    assert absent == fp(GAP_POINTS, od, None)
    assert absent != fp(GAP_POINTS, od, [])
    assert absent != fp(GAP_POINTS, od, [(42.0, 150.0)])
    assert fp(GAP_POINTS, od, [(42.0, 150.0)]) != fp(GAP_POINTS, od, [(41.0, 145.0)])
    assert fp(GAP_POINTS, od, [(42.0, 150.0)]) == fp(GAP_POINTS, od, [(42.0, 150.0)])
    assert 0 <= fp(GAP_POINTS, od, [(42.0, 150.0)]) < 2**64


def test_route_presence_is_a_distinct_cache_identity(livemap, monkeypatch):
    state = {"route": None}
    _stub_serving(livemap, monkeypatch)
    monkeypatch.setattr(livemap, "_fetch_route", lambda *_a: state["route"])
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)
    client = TestClient(livemap.app)

    plain = client.get("/path/42/estimate").json()
    state["route"] = (ROUTE_STR, PLAN_TS)
    with_route = client.get("/path/42/estimate").json()
    state["route"] = (ROUTE_STR_ALT, PLAN_TS)
    amended = client.get("/path/42/estimate").json()

    assert _gap_meta(plain)["uncertainty"]["bin"] == "gap_60_180m"
    assert _gap_meta(with_route)["route"]["tokens"] == 1
    assert _gap_meta(amended)["route"]["tokens"] == 2     # no stale hit on the 1-token entry
    assert len(livemap._est_cache) == 3
    state["route"] = None
    assert client.get("/path/42/estimate").json() == plain  # the pure-GC entry is still its own key


def test_route_query_patterns_survive_namespace_reserialization(livemap):
    # ET.tostring() rewrites the nxcm: prefix (live rows carry ns1:) — the r1 HIGH finding:
    # a literal-prefix pattern silently loses every element-text FlightRoute to pure GC
    import xml.etree.ElementTree as ET

    ns = "https://nas.faa.gov/schema/nas_common_messaging"
    flight_route = ET.Element(f"{{{ns}}}ncsmFlightRoute")
    el = ET.SubElement(flight_route, f"{{{ns}}}routeOfFlight")
    el.text = "KSEA..4800N/14000W..RCTP/1204"
    serialized = ET.tostring(flight_route, encoding="unicode")
    assert "nxcm:" not in serialized
    m = re.search(livemap.EST_ROUTE_TEXT_PAT, serialized)
    assert m is not None
    assert m.group(1) == "KSEA..4800N/14000W..RCTP/1204"

    amendment = ET.Element(f"{{{ns}}}amendmentData")
    ET.SubElement(amendment, f"{{{ns}}}newRouteOfFlight",
                  legacyFormat="KMDW..PXV.BLUZZ5.KMEM/0445")
    m2 = re.search(livemap.EST_ROUTE_ATTR_PAT, ET.tostring(amendment, encoding="unicode"))
    assert m2 is not None
    assert m2.group(1) == "KMDW..PXV.BLUZZ5.KMEM/0445"


def test_route_query_plan_selection_is_total_ordered(livemap):
    # same-timestamp raw-distinct twins survive the RMT — the pick must be deterministic (r1);
    # r4 prepends the O/D rank, r8 the plan-class rank; the order stays total
    assert "ORDER BY od_matches DESC, is_full DESC, msg_timestamp DESC, _dedup_fp DESC" \
        in livemap.EST_ROUTE_QUERY


def test_fingerprint_includes_plan_ts(livemap):
    pts = [(41.0, 150.0)]
    base = livemap.ess.input_fingerprint(GAP_POINTS, livemap.est.OD(), pts, 1000)
    assert livemap.ess.input_fingerprint(GAP_POINTS, livemap.est.OD(), pts, 2000) != base
    assert livemap.ess.input_fingerprint(GAP_POINTS, livemap.est.OD(), pts, 1000) == base


def test_ground_edged_hole_never_pays_the_swim_read(livemap, monkeypatch):
    # a ground stop is never bridged (gap_eligibility skips it) — it must not trigger the read (r1)
    ground_gap = [
        (1765500000, 35.0, 140.0, 0, 1, 10, 90, "adsblol"),
        (1765507200, 35.0, 140.1, 0, 1, 10, 90, "adsblol"),
    ]
    calls = _stub_serving(livemap, monkeypatch, result=_loader_result(points=ground_gap))
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)

    response = TestClient(livemap.app).get("/path/42/estimate")

    assert response.status_code == 200
    assert calls == []


def test_slow_or_motionless_holes_never_pay_the_swim_read(livemap, monkeypatch):
    # r2: the precheck IS the estimator's own eligibility now — kinematically dead or
    # motion-less gaps must not trigger the optional SWIM read
    slow = [
        (1765500000, 35.0, 140.0, 35000, 0, 450, 90, "adsblol"),
        (1765507200, 35.0, 140.1, 35000, 0, 450, 90, "adsblol"),
    ]
    calls = _stub_serving(livemap, monkeypatch, result=_loader_result(points=slow))
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)
    assert TestClient(livemap.app).get("/path/42/estimate").status_code == 200
    assert calls == []

    motionless = [
        (1765500000, 35.0, 140.0, 35000, 0, None, None, "adsblol"),
        (1765507200, 35.0, 160.0, 35000, 0, None, None, "adsblol"),
    ]
    monkeypatch.setattr(livemap, "_est_cache", {})
    calls2 = _stub_serving(livemap, monkeypatch, result=_loader_result(points=motionless))
    assert TestClient(livemap.app).get("/path/42/estimate").status_code == 200
    assert calls2 == []


def test_route_query_binds_the_plan_to_the_flights_own_leg(livemap):
    # r3: callsign-only latest-at-end selection let a neighboring same-callsign leg steal
    # the pick (measured live) — the filed-departure window pins the flight's own plan
    assert "filed_departure_time BETWEEN" in livemap.EST_ROUTE_QUERY
    assert "INTERVAL 6 HOUR" in livemap.EST_ROUTE_QUERY


def test_route_query_anchors_the_leg_on_reconciled_od(livemap):
    # r4: same-callsign connecting legs passed every geometry guard (~1,000 km displaced) —
    # the leg must anchor on reconciled O/D, exact ICAO or LID-one-prefix-short (CVG→KCVG)
    assert "od_matches >= 1" in livemap.EST_ROUTE_QUERY
    assert "od_conflicts = 0" in livemap.EST_ROUTE_QUERY
    # r5: constant-safe LID match (substring, never a bound-param LIKE haystack) and
    # coordinate-bearing plans out-rank terminal ./.-amendments that truncate the ocean
    assert "substring({origin:String}, 2)" in livemap.EST_ROUTE_QUERY
    assert "LIKE" not in livemap.EST_ROUTE_QUERY


def test_no_reconciled_endpoints_means_no_route_fetch(livemap, monkeypatch):
    calls = _stub_serving(livemap, monkeypatch)
    monkeypatch.setattr(livemap, "_fetch_od",
                        lambda _fid: (livemap.est.OD(), ("DAL69", 1765500000, 1765507200, None, None)))
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)

    assert TestClient(livemap.app).get("/path/42/estimate").status_code == 200
    assert calls == []


def test_route_bins_serve_floor_semantics(livemap):
    # adversarial r6: the bands were calibrated on GC bridges; an admitted prior can sit far
    # off that GC, so every _route bin serves its band as a floor (>= on the wire)
    for b in ("gap_2_10m_route", "gap_15_60m_route", "gap_60_180m_route", "gap_180m_plus_route"):
        assert livemap.ess.UNCERTAINTY_BANDS[b].get("floor") is True


def test_route_query_bounds_the_leading_pk(livemap):
    # adversarial r6 / manual P1: without a lower msg_timestamp bound, recent flights scan the
    # whole month partition (~20.6M rows measured); bounded A/B was 9.1x cheaper, results equal
    assert "msg_timestamp BETWEEN toDateTime({start:Int64}) - INTERVAL 30 HOUR" in livemap.EST_ROUTE_QUERY
