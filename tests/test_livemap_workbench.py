import datetime
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def livemap():
    spec = importlib.util.spec_from_file_location("livemap_app_wb", REPO_ROOT / "livemap" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _reset_wb_caches(livemap, monkeypatch):
    for name in ("_wb_airlines_cache", "_wb_services_cache", "_wb_instances_cache", "_wb_search_cache"):
        monkeypatch.setattr(livemap, name, {})


# ---- registration / gating ----

def test_private_route_table_includes_workbench(livemap):
    paths = {getattr(r, "path", None) for r in livemap.app.routes}
    assert "/features" in paths
    for p in ("/workbench/airlines", "/workbench/services", "/workbench/instances", "/workbench/search"):
        assert p in paths


def test_public_route_table_excludes_workbench(livemap_public):
    paths = {getattr(r, "path", None) for r in livemap_public.app.routes}
    assert "/features" not in paths
    assert not any((p or "").startswith("/workbench") for p in paths)


@pytest.mark.parametrize("path", [
    "/features", "/workbench/airlines", "/workbench/services",
    "/workbench/instances", "/workbench/search",
])
def test_public_workbench_paths_404(livemap_public, path):
    r = TestClient(livemap_public.app).get(path)
    assert r.status_code == 404


def test_public_does_not_serve_workbench_statics(livemap, livemap_public):
    asset = "/features/workbench/index.js"
    assert TestClient(livemap_public.app).get(asset).status_code == 404
    assert TestClient(livemap.app).get(asset).status_code == 200


@pytest.mark.parametrize("path", [
    "/features/workbench/index.js",
    "http://testserver//features/workbench/index.js",
    "/features/../features/workbench/index.js",
    "/x/../features/workbench/index.js",
    "/features/",
])
def test_public_static_deny_normalized_paths_and_methods(livemap_public, path):
    # the deny lives in the static layer (PublicStatic.get_response), post-normalization — a per-route
    # arm missed // prefixes and HEAD (found in review, PR #145)
    c = TestClient(livemap_public.app)
    assert c.get(path).status_code == 404
    assert c.head(path).status_code == 404


def test_private_static_class_is_unrestricted(livemap):
    c = TestClient(livemap.app)
    assert c.get("http://testserver//features/workbench/index.js").status_code == 200
    assert c.head("/features/workbench/index.js").status_code == 200


def test_features_contract(livemap):
    r = TestClient(livemap.app).get("/features")
    assert r.status_code == 200
    assert r.json() == {"features": {"workbench": True}}
    assert r.headers["cache-control"] == "no-store"


# ---- shapes (fetcher monkeypatched on the app module — pins ctx late-binding) ----

def test_airlines_shape(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    payload = {"airlines": [{"name": "ANA", "n_flights": 5, "n_services": 2,
                             "first_day": "2026-07-01", "last_day": "2026-07-30",
                             "tiers": {"settled": 3, "estimated": 1, "provisional": 1, "none": 0}}],
              "total": 1, "limit": 50, "offset": 0}
    monkeypatch.setattr(livemap, "_fetch_wb_airlines", lambda _q, _limit, _offset: payload)
    r = TestClient(livemap.app).get("/workbench/airlines")
    assert r.status_code == 200
    assert r.json() == payload
    assert r.headers["cache-control"] == "no-store"


def test_services_shape(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    payload = {"services": [{"callsign": "ANA1", "n_instances": 10,
                             "top_od": [{"o": "HND", "d": "ITM", "n": 5}],
                             "first_day": "2026-07-01", "last_day": "2026-07-30",
                             "tiers": {"settled": 5, "estimated": 3, "provisional": 1, "none": 1}}],
              "total": 1, "limit": 100, "offset": 0}
    monkeypatch.setattr(livemap, "_fetch_wb_services", lambda _airline, _q, _limit, _offset: payload)
    r = TestClient(livemap.app).get("/workbench/services")
    assert r.status_code == 200
    assert r.json() == payload
    assert r.headers["cache-control"] == "no-store"


def test_instances_shape(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    payload = {"instances": [{"flight_id": "12345678901234567890", "day": "2026-07-29",
                              "start_ts": 1.0, "end_ts": 2.0, "icao24": "abc123",
                              "registration": "JA123", "typecode": "B738", "callsign": "ANA1",
                              "airline": "ANA", "origin": {"icao": "RJTT", "iata": "HND", "city": "Tokyo"},
                              "dest": {"icao": "RJBB", "iata": "ITM", "city": "Osaka"},
                              "tier": "settled", "effective_gap_s": 10, "n_points": 500,
                              "is_military": False}],
              "od_breakdown": [{"o": "HND", "d": "ITM", "n": 5}], "total": 1, "limit": 50, "offset": 0}
    monkeypatch.setattr(livemap, "_fetch_wb_instances", lambda *_a, **_kw: payload)
    r = TestClient(livemap.app).get("/workbench/instances")
    assert r.status_code == 200
    assert r.json() == payload
    assert r.headers["cache-control"] == "no-store"
    # flight_id must survive the wire as a string — cityHash64 overflows JS Number
    assert isinstance(r.json()["instances"][0]["flight_id"], str)


def test_search_shape(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    payload = {"airlines": [{"name": "ANA", "n_flights": 10}],
              "services": [{"callsign": "ANA1", "airline": "ANA", "n_instances": 5}],
              "airframes": [{"icao24": "abc123", "registration": "JA123", "typecode": "B738",
                            "n_instances": 3}],
              "airports": [{"icao": "RJTT", "iata": "HND", "name": "Haneda", "city": "Tokyo"}]}
    monkeypatch.setattr(livemap, "_fetch_wb_search", lambda _q, _limit: payload)
    r = TestClient(livemap.app).get("/workbench/search?q=an")
    assert r.status_code == 200
    assert r.json() == payload
    assert r.headers["cache-control"] == "no-store"


def test_search_below_min_length_short_circuits(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)

    def boom(_q, _limit):
        raise AssertionError("q shorter than 2 chars must never reach the fetcher")

    monkeypatch.setattr(livemap, "_fetch_wb_search", boom)
    for q in ("", "a"):
        r = TestClient(livemap.app).get(f"/workbench/search?q={q}")
        assert r.status_code == 200
        assert r.json() == {"airlines": [], "services": [], "airframes": [], "airports": []}


# ---- pagination ----

def test_airlines_limit_clamped_offset_passthrough(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    seen = {}

    def fake(_q, limit, offset):
        seen["limit"], seen["offset"] = limit, offset
        return {"airlines": [], "total": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(livemap, "_fetch_wb_airlines", fake)
    TestClient(livemap.app).get("/workbench/airlines?limit=99999&offset=40")
    assert seen["limit"] == 200   # clamped to the airlines cap
    assert seen["offset"] == 40


def test_services_limit_clamped_offset_passthrough(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    seen = {}

    def fake(_airline, _q, limit, offset):
        seen["limit"], seen["offset"] = limit, offset
        return {"services": [], "total": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(livemap, "_fetch_wb_services", fake)
    TestClient(livemap.app).get("/workbench/services?limit=99999&offset=7")
    assert seen["limit"] == 500
    assert seen["offset"] == 7


def test_instances_limit_clamped_offset_passthrough(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    seen = {}

    def fake(*a, **_kw):
        seen["limit"], seen["offset"] = a[-2], a[-1]
        return {"instances": [], "od_breakdown": [], "total": 0, "limit": a[-2], "offset": a[-1]}

    monkeypatch.setattr(livemap, "_fetch_wb_instances", fake)
    TestClient(livemap.app).get("/workbench/instances?limit=99999&offset=13")
    assert seen["limit"] == 500
    assert seen["offset"] == 13


def test_search_limit_clamped(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    seen = {}

    def fake(_q, limit):
        seen["limit"] = limit
        return {"airlines": [], "services": [], "airframes": [], "airports": []}

    monkeypatch.setattr(livemap, "_fetch_wb_search", fake)
    TestClient(livemap.app).get("/workbench/search?q=ana&limit=99999")
    assert seen["limit"] == 100   # search has no spec-given cap number; 100 chosen defensively (see report)


# ---- bound-param safety: hostile values must never touch the SQL text ----

class _CapturingClient:
    def __init__(self):
        self.calls = []

    # rows must be plausible per query shape: the fetchers read result_rows[0][0] off the count, so an
    # all-empty double raises IndexError and every LATER query in the same fetcher goes uncaptured
    def query(self, sql, parameters=None, **_kw):
        self.calls.append((sql, parameters or {}))
        if sql.startswith("SELECT count()") or sql.startswith("SELECT uniqExact"):
            rows = [(0,)]
        elif "GROUP BY r.callsign ORDER BY n_instances" in sql:
            rows = [("ANA1", 1, "2026-07-01", "2026-07-01", 1, 0, 0, 0)]  # drives the top-OD follow-up query
        else:
            rows = []

        class _Res:
            result_rows = rows

        return _Res()

    def close(self):
        pass


def test_airlines_hostile_query_only_ever_bound(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    hostile = "'; DROP TABLE flights--\" 日本語"
    client = _CapturingClient()
    monkeypatch.setattr(livemap, "_ch_client", lambda: client)
    TestClient(livemap.app).get("/workbench/airlines", params={"q": hostile})
    assert len(client.calls) == 2   # list + count both ran; neither aborted before being checked
    for sql, _params in client.calls:
        assert hostile not in sql
    assert any(hostile in params.values() for _sql, params in client.calls)


def test_instances_hostile_reg_only_ever_bound(livemap, monkeypatch):
    # reg/callsign are upper-cased before binding — an all-uppercase hostile string survives that intact
    _reset_wb_caches(livemap, monkeypatch)
    hostile = "'; DROP TABLE--\" 日本語"
    client = _CapturingClient()
    monkeypatch.setattr(livemap, "_ch_client", lambda: client)
    TestClient(livemap.app).get("/workbench/instances", params={"reg": hostile})
    assert len(client.calls) == 3   # list + count + od-breakdown
    assert any("AS o," in sql for sql, _p in client.calls)   # the od-breakdown SQL was really reached
    for sql, _params in client.calls:
        assert hostile not in sql
    assert any(hostile in params.values() for _sql, params in client.calls)


def test_services_hostile_filters_only_ever_bound(livemap, monkeypatch):
    # q is upper-cased before binding, airline is not — an uppercase hostile string survives both
    _reset_wb_caches(livemap, monkeypatch)
    hostile = "'; DROP TABLE--\" 日本語"
    client = _CapturingClient()
    monkeypatch.setattr(livemap, "_ch_client", lambda: client)
    TestClient(livemap.app).get("/workbench/services", params={"airline": hostile, "q": hostile})
    assert len(client.calls) == 3   # list + count + top-OD
    assert any("rn <= 3" in sql for sql, _p in client.calls)   # the top-OD SQL was really reached
    for sql, _params in client.calls:
        assert hostile not in sql
    assert any(hostile in params.values() for _sql, params in client.calls)


def test_search_hostile_query_only_ever_bound(livemap, monkeypatch):
    # search fans the same q into five derived params (svc/hex/reg/code) — none may reach SQL text
    _reset_wb_caches(livemap, monkeypatch)
    hostile = "'; DROP TABLE--\" 日本語"
    client = _CapturingClient()
    monkeypatch.setattr(livemap, "_ch_client", lambda: client)
    TestClient(livemap.app).get("/workbench/search", params={"q": hostile})
    assert len(client.calls) == 4   # all four search queries ran with the hostile value bound
    for sql, _params in client.calls:
        assert hostile not in sql
    assert all(hostile in params.values() for _sql, params in client.calls)


# ---- caching ----

def test_airlines_cache_hit_then_miss_on_new_args(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    calls = {"n": 0}

    def fake(_q, limit, offset):
        calls["n"] += 1
        return {"airlines": [], "total": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(livemap, "_fetch_wb_airlines", fake)
    c = TestClient(livemap.app)
    c.get("/workbench/airlines?q=ana")
    c.get("/workbench/airlines?q=ana")
    assert calls["n"] == 1     # same args -> served from cache
    c.get("/workbench/airlines?q=jal")
    assert calls["n"] == 2     # different args -> re-invoked


def test_airlines_cache_ttl_expiry_reinvokes(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    # a fixed-value clock, not a 2-item iterator: the full ASGI stack (anyio/starlette) calls
    # time.time() an unpredictable extra number of times per request and would exhaust it
    clock = {"t": 1000.0}
    monkeypatch.setattr(livemap.time, "time", lambda: clock["t"])
    calls = {"n": 0}

    def fake(_q, limit, offset):
        calls["n"] += 1
        return {"airlines": [], "total": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(livemap, "_fetch_wb_airlines", fake)
    c = TestClient(livemap.app)
    c.get("/workbench/airlines?q=ana")
    clock["t"] = 1000.0 + livemap.WB_AIRLINES_CACHE_TTL_S + 1.0
    c.get("/workbench/airlines?q=ana")
    assert calls["n"] == 2     # the second call landed past TTL expiry


def test_services_cache_hit_then_miss_and_ttl_expiry(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(livemap.time, "time", lambda: clock["t"])
    calls = {"n": 0}

    def fake(_airline, _q, limit, offset):
        calls["n"] += 1
        return {"services": [], "total": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr(livemap, "_fetch_wb_services", fake)
    c = TestClient(livemap.app)
    c.get("/workbench/services?airline=ANA")
    c.get("/workbench/services?airline=ANA")
    assert calls["n"] == 1     # same args -> served from cache
    c.get("/workbench/services?airline=JAL")
    assert calls["n"] == 2     # different args -> re-invoked
    clock["t"] = 1000.0 + livemap.WB_SERVICES_CACHE_TTL_S + 1.0
    c.get("/workbench/services?airline=ANA")
    assert calls["n"] == 3     # past TTL -> re-invoked


def test_search_cache_hit_then_miss_and_ttl_expiry(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(livemap.time, "time", lambda: clock["t"])
    calls = {"n": 0}

    def fake(_q, _limit):
        calls["n"] += 1
        return {"airlines": [], "services": [], "airframes": [], "airports": []}

    monkeypatch.setattr(livemap, "_fetch_wb_search", fake)
    c = TestClient(livemap.app)
    c.get("/workbench/search?q=ana")
    c.get("/workbench/search?q=ana")
    assert calls["n"] == 1
    c.get("/workbench/search?q=jal")
    assert calls["n"] == 2
    clock["t"] = 1000.0 + livemap.WB_SEARCH_CACHE_TTL_S + 1.0
    c.get("/workbench/search?q=ana")
    assert calls["n"] == 3


def test_instances_cache_hit_then_miss_on_new_args(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    calls = {"n": 0}

    def fake(*a, **_kw):
        calls["n"] += 1
        return {"instances": [], "od_breakdown": [], "total": 0, "limit": a[-2], "offset": a[-1]}

    monkeypatch.setattr(livemap, "_fetch_wb_instances", fake)
    c = TestClient(livemap.app)
    c.get("/workbench/instances?callsign=ANA1")
    c.get("/workbench/instances?callsign=ANA1")
    assert calls["n"] == 1
    c.get("/workbench/instances?callsign=ANA2")
    assert calls["n"] == 2


# ---- degradation: never 500 ----

def test_airlines_generic_exception_serves_empty_200(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)

    def boom(_q, _limit, _offset):
        raise RuntimeError("ch down")

    monkeypatch.setattr(livemap, "_fetch_wb_airlines", boom)
    r = TestClient(livemap.app).get("/workbench/airlines?limit=50&offset=0")
    assert r.status_code == 200
    assert r.json() == {"airlines": [], "total": 0, "limit": 50, "offset": 0}


def test_services_generic_exception_serves_empty_200(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)

    def boom(*_a, **_kw):
        raise RuntimeError("ch down")

    monkeypatch.setattr(livemap, "_fetch_wb_services", boom)
    r = TestClient(livemap.app).get("/workbench/services?limit=100&offset=0")
    assert r.status_code == 200
    assert r.json() == {"services": [], "total": 0, "limit": 100, "offset": 0}


def test_instances_generic_exception_serves_empty_200(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)

    def boom(*_a, **_kw):
        raise RuntimeError("ch down")

    monkeypatch.setattr(livemap, "_fetch_wb_instances", boom)
    r = TestClient(livemap.app).get("/workbench/instances?limit=50&offset=0")
    assert r.status_code == 200
    assert r.json() == {"instances": [], "od_breakdown": [], "total": 0, "limit": 50, "offset": 0}


def test_search_generic_exception_serves_empty_200(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)

    def boom(_q, _limit):
        raise RuntimeError("ch down")

    monkeypatch.setattr(livemap, "_fetch_wb_search", boom)
    r = TestClient(livemap.app).get("/workbench/search?q=ana")
    assert r.status_code == 200
    assert r.json() == {"airlines": [], "services": [], "airframes": [], "airports": []}


class _FakeUnknownTableError(Exception):
    pass


def test_fetch_wb_instances_unknown_table_falls_back_to_tier_unknown(livemap, monkeypatch):
    # exercises the fetcher's OWN degradation (deploy-order guard), not the router's blanket catch
    def fake_query(sql, _parameters=None):
        class _Res:
            def __init__(self, rows):
                self.result_rows = rows

        if "fct_flight_recon_tier" in sql:
            raise _FakeUnknownTableError("Code: 60. DB::Exception: Table doesn't exist. (UNKNOWN_TABLE)")
        if sql.startswith("SELECT count()"):
            return _Res([(1,)])
        if "AS o," in sql:
            return _Res([("HND", "ITM", 1)])
        return _Res([(
            "1", "2026-07-29", None, None, "abc123", "JA123", "B738", "ANA1", "ANA",
            "RJTT", "HND", "Tokyo", "RJBB", "ITM", "Osaka", "unknown", None, None, 0,
        )])

    class FakeClient:
        def query(self, sql, parameters=None, **_kw):
            return fake_query(sql, parameters)

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: FakeClient())
    out = livemap._fetch_wb_instances("", "", "", "", "", "", "", False, None, None, "day_desc", 50, 0)
    assert out["instances"][0]["tier"] == "unknown"


def test_fetch_wb_instances_pins_lazy_materialization_off(livemap, monkeypatch):
    # without this setting the live tier-joined read raises NOT_FOUND_COLUMN_IN_BLOCK, and the
    # router's blanket catch would turn that into a permanently empty (but green) instances list
    seen = []

    class FakeClient:
        def query(self, sql, settings=None, **_kw):   # parameters= lands in _kw
            seen.append((sql, settings))

            class _Res:
                result_rows = [(0,)] if sql.startswith("SELECT count()") else []

            return _Res()

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: FakeClient())
    for sort in ("day_desc", "day_asc"):
        livemap._fetch_wb_instances("", "", "", "", "", "", "", False, None, None, sort, 50, 0)
    main = [s for sql, s in seen if sql.startswith("SELECT toString(") and "fct_flight_recon_tier" in sql]
    assert len(main) == 2
    assert all(s and s.get("query_plan_optimize_lazy_materialization") == 0 for s in main)


def test_paged_queries_carry_total_order_tiebreaks(livemap):
    # ties are everywhere (19k+ duplicated start_time values measured) — a tie-break-free ORDER BY
    # makes LIMIT/OFFSET pages serve a row twice and drop another entirely
    wb = livemap.wb
    for q in (wb.INSTANCES_QUERY_TIER_DESC, wb.INSTANCES_QUERY_TIER_ASC,
              wb.INSTANCES_QUERY_NO_TIER_DESC, wb.INSTANCES_QUERY_NO_TIER_ASC):
        assert "r.flight_id" in q.split("ORDER BY")[1]
    for q in (wb.AIRLINES_QUERY_TIER, wb.AIRLINES_QUERY_NO_TIER):
        assert "ORDER BY n_flights DESC, name" in q
    for q in (wb.SERVICES_QUERY_TIER, wb.SERVICES_QUERY_NO_TIER):
        assert "ORDER BY n_instances DESC, callsign" in q


def test_tier_join_misses_normalize_to_unknown(livemap):
    # join_use_nulls is off, so an unmatched LEFT JOIN row carries '' rather than NULL
    wb = livemap.wb
    assert "if(coalesce(t.tier, '') = '', 'unknown', t.tier)" in wb.INSTANCES_QUERY_TIER_DESC
    for q in (wb.AIRLINES_QUERY_TIER, wb.SERVICES_QUERY_TIER):
        assert "countIf(coalesce(t.tier, '') IN ('', 'none'))" in q


def test_fetch_wb_instances_military_unavailable_without_tier_mart(livemap, monkeypatch):
    class FakeClient:
        def query(self, _sql, _parameters=None, **_kw):
            raise _FakeUnknownTableError("Code: 60. UNKNOWN_TABLE")

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: FakeClient())
    out = livemap._fetch_wb_instances("", "", "", "", "", "", "", True, None, None, "day_desc", 50, 0)
    assert out == {"instances": [], "od_breakdown": [], "total": 0, "limit": 50, "offset": 0,
                   "military_filter_available": False}


# ---- LADD private pin: the workbench never filters, extends the 17-pin family ----

def test_private_instances_ladd_listed_hex_unfiltered(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    monkeypatch.setattr(livemap, "_ladd_suppress",
                        {"hex": frozenset({"abc123"}), "callsign": frozenset({"SECRET1"})})
    payload = {"instances": [{"flight_id": "1", "day": "2026-07-29", "start_ts": 1.0, "end_ts": 2.0,
                              "icao24": "abc123", "registration": "JA123", "typecode": "B738",
                              "callsign": "SECRET1", "airline": "ANA",
                              "origin": {"icao": "RJTT", "iata": "HND", "city": "Tokyo"},
                              "dest": {"icao": "RJBB", "iata": "ITM", "city": "Osaka"},
                              "tier": "settled", "effective_gap_s": 10, "n_points": 100,
                              "is_military": False}],
              "od_breakdown": [], "total": 1, "limit": 50, "offset": 0}
    monkeypatch.setattr(livemap, "_fetch_wb_instances", lambda *_a, **_kw: payload)
    r = TestClient(livemap.app).get("/workbench/instances?hex=abc123")
    assert r.json() == payload   # no LADD filtering exists anywhere on the workbench path


def test_private_search_ladd_listed_airframe_unfiltered(livemap, monkeypatch):
    _reset_wb_caches(livemap, monkeypatch)
    monkeypatch.setattr(livemap, "_ladd_suppress", {"hex": frozenset({"abc123"}), "callsign": frozenset()})
    payload = {"airlines": [], "services": [],
              "airframes": [{"icao24": "abc123", "registration": "JA123", "typecode": "B738",
                            "n_instances": 3}],
              "airports": []}
    monkeypatch.setattr(livemap, "_fetch_wb_search", lambda _q, _limit: payload)
    r = TestClient(livemap.app).get("/workbench/search?q=ab")
    assert r.json() == payload


# ---- pure helpers ----

def test_parse_day_roundtrips_and_degrades_to_none(livemap):
    wb = livemap.wb
    assert wb.parse_day("2026-07-29") == datetime.date(2026, 7, 29)
    assert wb.parse_day("") is None
    assert wb.parse_day(None) is None
    assert wb.parse_day("not-a-date") is None


def test_clamp_floors_and_caps(livemap):
    wb = livemap.wb
    assert wb.clamp(-5, 200) == 0
    assert wb.clamp(99999, 200) == 200
    assert wb.clamp(10, 200) == 10


# ---- Dockerfile / file layout ----

def test_workbench_files_exist_next_to_app():
    livemap_dir = REPO_ROOT / "livemap"
    assert (livemap_dir / "workbench.py").exists()
    assert (livemap_dir / "routes_workbench.py").exists()


# ---- live-CH query-form execution (both tier variants), same skip/connect semantics as ch_cur ----

def _harmless_instances_params(livemap, military=0):
    wb = livemap.wb
    p = wb.instances_params("", "", "", "", "", "", "", military,
                            datetime.date(2020, 1, 1), datetime.date(2026, 12, 31))
    p["limit"] = 2
    p["offset"] = 0
    return p


def _live_ch_client():
    # same skip/connect pattern as test_livemap_route_query_live.py: only connectivity skips, every
    # other failure is real (tier-mart absence is the one expected exception, tolerated explicitly).
    try:
        import clickhouse_connect
        from clickhouse_connect.driver import exceptions as ch_exc
    except ImportError as exc:
        pytest.skip(f"clickhouse-connect unavailable: {exc}")
    import os
    try:
        return clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
    except (ch_exc.OperationalError, OSError) as exc:
        pytest.skip(f"live ClickHouse unreachable: {exc}")


def test_workbench_sql_executes_on_live_ch(livemap):
    client = _live_ch_client()
    wb = livemap.wb
    qs = wb.INSTANCES_QUERY_SETTINGS   # the fetcher's own object, so the two can't drift

    def run(sql, params, settings=None):
        try:
            client.query(sql, parameters=params, settings=settings)
        except Exception as exc:  # tier-mart may be absent (deploy order) — tolerate only that
            assert livemap._is_unknown_table_error(exc), f"unexpected live-CH failure: {exc}"

    try:
        run(wb.AIRLINES_QUERY_TIER, {"q": "", "limit": 2, "offset": 0})
        run(wb.AIRLINES_QUERY_NO_TIER, {"q": "", "limit": 2, "offset": 0})
        run(wb.AIRLINES_COUNT_QUERY, {"q": ""})

        run(wb.SERVICES_QUERY_TIER, {"airline": "", "q": "A", "limit": 2, "offset": 0})
        run(wb.SERVICES_QUERY_NO_TIER, {"airline": "", "q": "A", "limit": 2, "offset": 0})
        run(wb.SERVICES_COUNT_QUERY, {"airline": "", "q": "A"})
        run(wb.SERVICES_TOP_OD_QUERY, {"callsigns": ["ZZZZ99"]})

        iparams = _harmless_instances_params(livemap)
        run(wb.INSTANCES_QUERY_TIER_DESC, iparams, qs)
        run(wb.INSTANCES_QUERY_TIER_ASC, iparams, qs)
        run(wb.INSTANCES_QUERY_NO_TIER_DESC, iparams, qs)
        run(wb.INSTANCES_QUERY_NO_TIER_ASC, iparams, qs)
        run(wb.INSTANCES_COUNT_QUERY_TIER, iparams)
        run(wb.INSTANCES_COUNT_QUERY_NO_TIER, iparams)
        run(wb.INSTANCES_OD_BREAKDOWN_QUERY_TIER, iparams)
        run(wb.INSTANCES_OD_BREAKDOWN_QUERY_NO_TIER, iparams)
        run(wb.INSTANCES_QUERY_TIER_DESC, _harmless_instances_params(livemap, military=1), qs)

        sparams = wb.search_params("AN")
        sparams["limit"] = 2
        run(wb.SEARCH_AIRLINES_QUERY, sparams)
        run(wb.SEARCH_SERVICES_QUERY, sparams)
        run(wb.SEARCH_AIRFRAMES_QUERY, sparams)
        run(wb.SEARCH_AIRPORTS_QUERY, sparams)
    finally:
        client.close()


def test_instances_day_filter_windows_on_jst_not_utc(livemap):
    # value oracle for the day seam: an instance starting >= 15:00 UTC belongs to the NEXT JST calendar
    # day, so the same flight must be inside a day_from/day_to of its JST day and outside its UTC one.
    client = _live_ch_client()
    wb = livemap.wb
    try:
        probe = client.query(
            "SELECT toString(flight_id), lower(icao24), toDate(start_time, 'Asia/Tokyo'), toDate(start_time) "
            f"FROM {wb.RECON_TBL} WHERE icao24 IS NOT NULL AND toHour(start_time) >= 15 "
            "ORDER BY start_time DESC LIMIT 1"
        ).result_rows
        if not probe:
            pytest.skip("no reconciled flight starting at/after 15:00 UTC to probe")
        fid, hex_, jst_day, utc_day = probe[0]
        assert jst_day == utc_day + datetime.timedelta(days=1)

        def ids_for(day):
            p = wb.instances_params("", "", hex_, "", "", "", "", 0, day, day)
            p["limit"], p["offset"] = 500, 0
            rows = client.query(wb.INSTANCES_QUERY_NO_TIER_DESC, parameters=p,
                                settings=wb.INSTANCES_QUERY_SETTINGS).result_rows
            return {r[0]: r[1] for r in rows}

        on_jst = ids_for(jst_day)
        assert fid in on_jst
        assert on_jst[fid] == jst_day.isoformat()   # the served `day` column is the JST one too
        assert fid not in ids_for(utc_day)
    finally:
        client.close()
