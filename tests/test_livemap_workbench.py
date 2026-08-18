import datetime
import importlib.util
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _wb_caches_reset(livemap, monkeypatch):
    # every test shares the module-scoped private app — start each from cold store caches
    for name in livemap.wb_store.caches:
        monkeypatch.setitem(livemap.wb_store.caches, name, {})


# ---- registration / gating ----

def test_private_route_table_includes_workbench(livemap):
    paths = {getattr(r, "path", None) for r in livemap.app.routes}
    assert "/features" in paths
    for p in ("/workbench/airlines", "/workbench/services", "/workbench/instances", "/workbench/search",
              "/workbench/summary", "/workbench/trends", "/workbench/flags",
              "/workbench/estimates", "/workbench/coverage"):
        assert p in paths


def test_public_route_table_excludes_workbench(livemap_public):
    paths = {getattr(r, "path", None) for r in livemap_public.app.routes}
    assert "/features" not in paths
    assert not any((p or "").startswith("/workbench") for p in paths)


@pytest.mark.parametrize("path", [
    "/features", "/workbench/airlines", "/workbench/services",
    "/workbench/instances", "/workbench/search",
    "/workbench/summary", "/workbench/trends", "/workbench/flags",
    "/workbench/estimates", "/workbench/coverage",
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
    "/features/workbench/chunks/x-abc.js",
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
    assert r.json() == {"features": {"workbench": True}, "contract": livemap.WB_CONTRACT}
    assert r.headers["cache-control"] == "no-store"


# ---- endpoint contract tables (fetcher monkeypatched on the store instance — handlers resolve
# ---- it at request time); per-endpoint one-off asserts stay as dedicated tests below ----

_SHAPE_CASES = {
    "airlines": ("/workbench/airlines",
                 {"airlines": [{"name": "ANA", "n_flights": 5, "n_services": 2,
                                "first_day": "2026-07-01", "last_day": "2026-07-30",
                                "tiers": {"settled": 3, "estimated": 1, "provisional": 1, "none": 0}}],
                  "total": 1, "limit": 50, "offset": 0}),
    "services": ("/workbench/services",
                 {"services": [{"callsign": "ANA1", "n_instances": 10,
                                "top_od": [{"o": "HND", "d": "ITM", "n": 5}],
                                "first_day": "2026-07-01", "last_day": "2026-07-30",
                                "tiers": {"settled": 5, "estimated": 3, "provisional": 1, "none": 1}}],
                  "total": 1, "limit": 100, "offset": 0}),
    "instances": ("/workbench/instances",
                  {"instances": [{"flight_id": "12345678901234567890", "day": "2026-07-29",
                                  "start_ts": 1.0, "end_ts": 2.0, "icao24": "abc123",
                                  "registration": "JA123", "typecode": "B738", "callsign": "ANA1",
                                  "airline": "ANA",
                                  "origin": {"icao": "RJTT", "iata": "HND", "city": "Tokyo"},
                                  "dest": {"icao": "RJBB", "iata": "ITM", "city": "Osaka"},
                                  "tier": "settled", "effective_gap_s": 10, "n_points": 500,
                                  "is_military": False}],
                   "od_breakdown": [{"o": "HND", "d": "ITM", "n": 5}],
                   "total": 1, "limit": 50, "offset": 0}),
    "search": ("/workbench/search?q=an",
               {"airlines": [{"name": "ANA", "n_flights": 10}],
                "services": [{"callsign": "ANA1", "airline": "ANA", "n_instances": 5}],
                "airframes": [{"icao24": "abc123", "registration": "JA123", "typecode": "B738",
                               "n_instances": 3}],
                "airports": [{"icao": "RJTT", "iata": "HND", "name": "Haneda", "city": "Tokyo"}]}),
    "summary": ("/workbench/summary",
                {"flights": 178553, "aircraft": 6893, "services": 10784,
                 "daily": [["2026-07-10", 5434]],
                 "flags": {"available": True, "flagged": 22259,
                           "classes": {"tiebreak_endpoint": 8400, "diversion": 487}},
                 "tiers": {"available": True, "mix": {"settled": 101673, "estimated": 68834},
                           "daily": [["2026-07-10", {"settled": 3511, "estimated": 1923}]]},
                 "est": {"available": True, "err_p50_km": 0.57, "n": 232,
                         "daily": [["2026-07-24", 4.47, 10]]},
                 "movers": [{"key": "GMP-CJU", "n": 3072, "prev_n": 2734, "delta_pct": 12.4}]}),
    "trends": ("/workbench/trends",
               {"dim": "route", "grain": "day",
                "series": [{"key": "GMP-CJU", "points": [["2026-07-10", 98]]}],
                "rank": [{"key": "GMP-CJU", "n": 3072, "distinct_aircraft": 289, "prev_n": 2734,
                          "delta_pct": 12.4}],
                "total": 493, "limit": 20, "offset": 0}),
    "flags": ("/workbench/flags",
              {"available": True,
               "flags": [{"flight_id": "12345678901234567890", "day": "2026-07-29",
                          "start_ts": 1.0, "end_ts": 2.0, "icao24": "abc123",
                          "registration": "JA123", "typecode": "B738", "callsign": "ANA1",
                          "airline": "ANA", "origin": {"icao": "RJTT", "iata": "HND", "city": "Tokyo"},
                          "dest": {"icao": "RJBB", "iata": "ITM", "city": "Osaka"},
                          "tier": "settled", "effective_gap_s": 10, "n_points": 500,
                          "is_military": False, "flag_class": "diversion",
                          "detail": "dest RJNA vs modal RJGG 79/87"}],
               "classes": {"one_sided_intl": 6922, "single_source": 5662},
               "total": 486, "limit": 50, "offset": 0}),
    "estimates": ("/workbench/estimates",
                  {"available": True,
                   "headline": [{"config_hash": "2537707548349448576", "n": 15, "p50_km": 0.316,
                                 "p90_km": 2.831, "first_day": "2026-07-29", "last_day": "2026-07-29"}],
                   "daily": [{"day": "2026-07-29", "config_hash": "2537707548349448576",
                              "p50_km": 0.316, "p90_km": 2.831, "n": 15}],
                   "mix": {"available": True,
                           "skip": [{"value": "gap:on_ground_edge", "producer": "serving-private",
                                     "n": 23}],
                           "segment_kind": [{"value": "gap", "producer": "serving", "n": 68}],
                           "uncertainty_bin": [{"value": "dr", "producer": "serving", "n": 29}]},
                   "outcomes": {"settled": 232, "awaiting": 93, "ambiguous": 8},
                   "input_split": {"provisional": 258, "settled": 75}}),
    "coverage": ("/workbench/coverage",
                 {"available": True,
                  "tier_daily": [["2026-08-05", {"settled": 3809, "estimated": 2556}]],
                  "gap_bins": [{"ge": 0, "lt": 60, "n": 2100},
                               {"ge": 43200, "lt": None, "n": 2}],
                  "observed": [{"day": "2026-08-05", "median": 0.0942, "n": 6365}]}),
}


@pytest.mark.parametrize("name", list(_SHAPE_CASES))
def test_endpoint_shape(livemap, monkeypatch, name):
    path, payload = _SHAPE_CASES[name]
    monkeypatch.setattr(livemap.wb_store, f"fetch_{name}", lambda *_a, **_kw: payload)
    r = TestClient(livemap.app).get(path)
    assert r.status_code == 200
    assert r.json() == payload
    assert r.headers["cache-control"] == "no-store"


def test_instances_flight_id_is_str(livemap, monkeypatch):
    # flight_id must survive the wire as a string — cityHash64 overflows JS Number
    path, payload = _SHAPE_CASES["instances"]
    monkeypatch.setattr(livemap.wb_store, "fetch_instances", lambda *_a, **_kw: payload)
    r = TestClient(livemap.app).get(path)
    assert isinstance(r.json()["instances"][0]["flight_id"], str)


def test_search_below_min_length_short_circuits(livemap, monkeypatch):
    def boom(_q, _limit):
        raise AssertionError("q shorter than 2 chars must never reach the fetcher")

    monkeypatch.setattr(livemap.wb_store, "fetch_search", boom)
    for q in ("", "a"):
        r = TestClient(livemap.app).get(f"/workbench/search?q={q}")
        assert r.status_code == 200
        assert r.json() == {"airlines": [], "services": [], "airframes": [], "airports": []}


# ---- pagination ----

# (query string, clamped limit, offset passthrough or None for offset-less search); limits are the
# per-endpoint caps — airlines 200, trends 50, search 100 (defensive, no spec number), else 500.
_CLAMP_CASES = {
    "airlines": ("/workbench/airlines?limit=99999&offset=40", 200, 40),
    "services": ("/workbench/services?limit=99999&offset=7", 500, 7),
    "instances": ("/workbench/instances?limit=99999&offset=13", 500, 13),
    "search": ("/workbench/search?q=ana&limit=99999", 100, None),
    "trends": ("/workbench/trends?limit=99999&offset=60&dim=airport&grain=hour", 50, 60),
    "flags": ("/workbench/flags?limit=99999&offset=11&class=diversion", 500, 11),
}


@pytest.mark.parametrize("name", list(_CLAMP_CASES))
def test_limit_clamped_offset_passthrough(livemap, monkeypatch, name):
    path, want_limit, want_offset = _CLAMP_CASES[name]
    seen = {}

    def fake(*a, **_kw):
        seen["args"] = a
        return {}

    monkeypatch.setattr(livemap.wb_store, f"fetch_{name}", fake)
    TestClient(livemap.app).get(path)
    if want_offset is None:
        assert seen["args"][-1] == want_limit
    else:
        assert seen["args"][-2:] == (want_limit, want_offset)


def test_trends_dim_passes_through_to_fetcher(livemap, monkeypatch):
    seen = {}

    def fake(dim, _df, _dt, limit, offset):
        seen["dim"] = dim
        return {"dim": dim, "grain": "day", "series": [], "rank": [], "total": 0,
                "limit": limit, "offset": offset}

    monkeypatch.setattr(livemap.wb_store, "fetch_trends", fake)
    TestClient(livemap.app).get("/workbench/trends?dim=airport&grain=hour")
    assert seen["dim"] == "airport"


def test_trends_unknown_dim_normalizes_to_route(livemap, monkeypatch):
    seen = {}

    def fake(dim, _df, _dt, limit, offset):
        seen["dim"] = dim
        return {"dim": dim, "grain": "day", "series": [], "rank": [], "total": 0,
                "limit": limit, "offset": offset}

    monkeypatch.setattr(livemap.wb_store, "fetch_trends", fake)
    r = TestClient(livemap.app).get("/workbench/trends?dim=nonsense")
    assert seen["dim"] == "route"
    assert r.json()["dim"] == "route"


def test_flags_class_alias_reaches_fetcher(livemap, monkeypatch):
    seen = {}

    def fake(class_, _df, _dt, limit, offset):
        seen["class"] = class_
        return {"available": True, "flags": [], "classes": {}, "total": 0,
                "limit": limit, "offset": offset}

    monkeypatch.setattr(livemap.wb_store, "fetch_flags", fake)
    TestClient(livemap.app).get("/workbench/flags?class=diversion")
    assert seen["class"] == "diversion"   # `class` is a Python keyword — it rides an alias


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


# hostile strings survive reg/callsign/q normalization intact; marker pins that the endpoint's
# LAST query form really ran, and every_bound=True (search) demands the value on ALL queries.
_HOSTILE_CASES = {
    "airlines": {"params": {"q": "'; DROP TABLE flights--\" 日本語"},
                 "n_calls": 2,   # list + count both ran; neither aborted before being checked
                 "marker": None, "every_bound": False},
    "instances": {"params": {"reg": "'; DROP TABLE--\" 日本語"},
                  "n_calls": 3,   # list + count + od-breakdown
                  "marker": "AS o,", "every_bound": False},
    "services": {"params": {"airline": "'; DROP TABLE--\" 日本語", "q": "'; DROP TABLE--\" 日本語"},
                 "n_calls": 3,   # list + count + top-OD
                 "marker": "rn <= 3", "every_bound": False},
    # search fans the same q into five derived params (svc/hex/reg/code) — none may reach SQL text
    "search": {"params": {"q": "'; DROP TABLE--\" 日本語"},
               "n_calls": 4,   # all four search queries ran with the hostile value bound
               "marker": None, "every_bound": True},
    "flags": {"params": {"class": "'; DROP TABLE--\" 日本語"},
              "n_calls": 3,   # feed + count + class histogram
              "marker": "GROUP BY f.flag_class", "every_bound": False},
}


@pytest.mark.parametrize("name", list(_HOSTILE_CASES))
def test_hostile_filters_only_ever_bound(livemap, monkeypatch, name):
    case = _HOSTILE_CASES[name]
    hostile_values = set(case["params"].values())
    client = _CapturingClient()
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    TestClient(livemap.app).get(f"/workbench/{name}", params=case["params"])
    assert len(client.calls) == case["n_calls"]
    if case["marker"]:
        assert any(case["marker"] in sql for sql, _p in client.calls)
    for sql, _params in client.calls:
        assert all(hostile not in sql for hostile in hostile_values)
    check = all if case["every_bound"] else any
    assert check(any(hostile in params.values() for hostile in hostile_values)
                 for _sql, params in client.calls)


def test_trends_hostile_dim_never_reaches_sql_or_params(livemap, monkeypatch):
    # dim is not bound at all — it only picks among three pre-built query texts, so a hostile
    # value must vanish at the Python layer rather than survive as a parameter
    hostile = "'; DROP--"
    client = _CapturingClient()
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    out = livemap.wb_store.fetch_trends(hostile, None, None, 20, 0)
    assert out["dim"] == "route"
    assert client.calls
    for sql, params in client.calls:
        assert hostile not in sql
        assert hostile not in params.values()


# ---- caching ----

# (repeated request, different-args request) per endpoint
_CACHE_CASES = {
    "airlines": ("/workbench/airlines?q=ana", "/workbench/airlines?q=jal"),
    "services": ("/workbench/services?airline=ANA", "/workbench/services?airline=JAL"),
    "instances": ("/workbench/instances?callsign=ANA1", "/workbench/instances?callsign=ANA2"),
    "search": ("/workbench/search?q=ana", "/workbench/search?q=jal"),
    "summary": ("/workbench/summary?day_from=2026-07-10&day_to=2026-08-08",
                "/workbench/summary?day_from=2026-07-11&day_to=2026-08-08"),
    "trends": ("/workbench/trends?dim=route", "/workbench/trends?dim=airline"),
    "flags": ("/workbench/flags?class=diversion", "/workbench/flags?class=military"),
    "estimates": ("/workbench/estimates?day_from=2026-07-10&day_to=2026-08-08",
                  "/workbench/estimates?day_from=2026-07-11&day_to=2026-08-08"),
    "coverage": ("/workbench/coverage?day_from=2026-07-10&day_to=2026-08-08",
                 "/workbench/coverage?day_from=2026-07-11&day_to=2026-08-08"),
}


@pytest.mark.parametrize("name", list(_CACHE_CASES))
def test_cache_hit_then_miss_and_ttl_expiry(livemap, monkeypatch, name):
    path_a, path_b = _CACHE_CASES[name]
    # a fixed-value clock, not a 2-item iterator: the full ASGI stack (anyio/starlette) calls
    # time.time() an unpredictable extra number of times per request and would exhaust it
    clock = {"t": 1000.0}
    monkeypatch.setattr(livemap.time, "time", lambda: clock["t"])
    calls = {"n": 0}

    def fake(*_a, **_kw):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(livemap.wb_store, f"fetch_{name}", fake)
    c = TestClient(livemap.app)
    c.get(path_a)
    c.get(path_a)
    assert calls["n"] == 1     # same args -> served from cache
    c.get(path_b)
    assert calls["n"] == 2     # different args -> re-invoked
    clock["t"] = 1000.0 + livemap.wb_store.ttls[name] + 1.0
    c.get(path_a)
    assert calls["n"] == 3     # past TTL -> re-invoked


def test_trends_grain_not_in_cache_key(livemap, monkeypatch):
    calls = {"n": 0}

    def fake(dim, _df, _dt, limit, offset):
        calls["n"] += 1
        return livemap.wb.empty_trends(dim, limit, offset)

    monkeypatch.setattr(livemap.wb_store, "fetch_trends", fake)
    c = TestClient(livemap.app)
    c.get("/workbench/trends?dim=route")
    c.get("/workbench/trends?dim=route&grain=hour")
    assert calls["n"] == 1     # grain is ignored and must not be part of the key


# ---- degradation: never 500 ----

# summary/trends carry complete:False (transient-outage signal, distinct from a section's
# available:false = mart not deployed); flags keeps available:True for the same reason
_NEVER_500_CASES = {
    "airlines": ("/workbench/airlines?limit=50&offset=0",
                 {"airlines": [], "total": 0, "limit": 50, "offset": 0}),
    "services": ("/workbench/services?limit=100&offset=0",
                 {"services": [], "total": 0, "limit": 100, "offset": 0}),
    "instances": ("/workbench/instances?limit=50&offset=0",
                  {"instances": [], "od_breakdown": [], "total": 0, "limit": 50, "offset": 0}),
    "search": ("/workbench/search?q=ana",
               {"airlines": [], "services": [], "airframes": [], "airports": []}),
    "summary": ("/workbench/summary",
                {"flights": 0, "aircraft": 0, "services": 0, "daily": [],
                 "flags": {"available": False, "flagged": 0, "classes": {}},
                 "tiers": {"available": False, "mix": {}, "daily": []},
                 "est": {"available": False, "err_p50_km": None, "n": 0, "daily": []},
                 "movers": [], "complete": False}),
    "trends": ("/workbench/trends?dim=airline&limit=20&offset=0",
               {"dim": "airline", "grain": "day", "series": [], "rank": [], "total": 0,
                "limit": 20, "offset": 0, "complete": False}),
    "flags": ("/workbench/flags?limit=50&offset=0",
              {"available": True, "complete": False, "flags": [], "classes": {}, "total": 0,
               "limit": 50, "offset": 0}),
    "estimates": ("/workbench/estimates",
                  {"available": True, "complete": False, "headline": [], "daily": [],
                   "mix": {"available": True, "skip": [], "segment_kind": [], "uncertainty_bin": []},
                   "outcomes": {"settled": 0, "awaiting": 0, "ambiguous": 0},
                   "input_split": {"provisional": 0, "settled": 0}}),
    "coverage": ("/workbench/coverage",
                 {"available": True, "complete": False, "tier_daily": [], "gap_bins": [],
                  "observed": []}),
}


@pytest.mark.parametrize("name", list(_NEVER_500_CASES))
def test_generic_exception_serves_empty_200(livemap, monkeypatch, name):
    path, want = _NEVER_500_CASES[name]

    def boom(*_a, **_kw):
        raise RuntimeError("ch down")

    monkeypatch.setattr(livemap.wb_store, f"fetch_{name}", boom)
    r = TestClient(livemap.app).get(path)
    assert r.status_code == 200
    assert r.json() == want


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

    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: FakeClient())
    out = livemap.wb_store.fetch_instances("", "", "", "", "", "", "", False, None, None, "day_desc", 50, 0)
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

    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: FakeClient())
    for sort in ("day_desc", "day_asc"):
        livemap.wb_store.fetch_instances("", "", "", "", "", "", "", False, None, None, sort, 50, 0)
    main = [s for sql, s in seen if sql.startswith("SELECT toString(") and "fct_flight_recon_tier" in sql]
    assert len(main) == 2
    assert all(s and s.get("query_plan_optimize_lazy_materialization") == 0 for s in main)


def test_paged_queries_carry_total_order_tiebreaks(livemap):
    # ties are everywhere (19k+ duplicated start_time values measured) — a tie-break-free ORDER BY
    # makes LIMIT/OFFSET pages serve a row twice and drop another entirely
    wb = livemap.wb
    for q in (wb.instances_query(tier=tier, asc=asc) for tier in (True, False) for asc in (True, False)):
        assert "r.flight_id" in q.split("ORDER BY")[1]
    for q in (wb.AIRLINES_QUERY_TIER, wb.AIRLINES_QUERY_NO_TIER):
        assert "ORDER BY n_flights DESC, name" in q
    for q in (wb.SERVICES_QUERY_TIER, wb.SERVICES_QUERY_NO_TIER):
        assert "ORDER BY n_instances DESC, callsign" in q
    for q in (wb.FLAGS_QUERY_TIER, wb.FLAGS_QUERY_NO_TIER):
        assert "ORDER BY r.start_time DESC, r.flight_id DESC, f.flag_class ASC" in q
    for dim in ("route", "airline", "airport"):
        assert "ORDER BY n DESC, k" in wb.TRENDS_RANK_QUERY[dim]


def test_summary_est_arms_follow_the_standing_drift_read(livemap):
    # measured on live CH for a 30 d window: raw rows read p50 0.572 km / n 232, the deduped pool
    # 1.753 km / n 147 — median-of-medians over repeated inputs hides the drift the tile exists for
    est = livemap.wb.SUMMARY_EST_ARMS
    assert "skip_ambiguous = 0" in est
    assert "ORDER BY computed_at, estimate_id LIMIT 1 BY input_fingerprint, seg_idx" in est
    assert "arraySort(groupArrayArray(errs_km)) AS pool" in est
    assert "quantileExact" not in est


def test_tier_join_misses_normalize_to_unknown(livemap):
    # join_use_nulls is off, so an unmatched LEFT JOIN row carries '' rather than NULL
    wb = livemap.wb
    assert "if(coalesce(t.tier, '') = '', 'unknown', t.tier)" in wb.instances_query(tier=True, asc=False)
    for q in (wb.AIRLINES_QUERY_TIER, wb.SERVICES_QUERY_TIER):
        assert "countIf(coalesce(t.tier, '') IN ('', 'none'))" in q


def test_fetch_wb_instances_military_unavailable_without_tier_mart(livemap, monkeypatch):
    class FakeClient:
        def query(self, _sql, _parameters=None, **_kw):
            raise _FakeUnknownTableError("Code: 60. UNKNOWN_TABLE")

        def close(self):
            pass

    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: FakeClient())
    out = livemap.wb_store.fetch_instances("", "", "", "", "", "", "", True, None, None, "day_desc", 50, 0)
    assert out == {"instances": [], "od_breakdown": [], "total": 0, "limit": 50, "offset": 0,
                   "military_filter_available": False}


_INSTANCE_COLS = (
    "1", "2026-07-29", None, None, "abc123", "JA123", "B738", "ANA1", "ANA",
    "RJTT", "HND", "Tokyo", "RJBB", "ITM", "Osaka", "unknown", None, None, 0,
)
_FLAG_ROW = _INSTANCE_COLS + ("diversion", "dest RJNA vs modal RJGG 79/87")


class _LadderClient:
    # A fake CH whose only behaviour is "these table names don't exist" — drives the fetchers'
    # unknown-table degradation ladders without needing a live warehouse.
    def __init__(self, missing, present=()):
        self.missing, self.present, self.sqls = missing, present, []

    def query(self, sql, **_kw):
        self.sqls.append(sql)
        rows = []
        if "system.tables" in sql:
            rows = [(n,) for n in self.present]
        else:
            for name in self.missing:
                if name in sql:
                    raise _FakeUnknownTableError("Code: 60. DB::Exception: (UNKNOWN_TABLE)")
            if sql.startswith("SELECT count()") or sql.startswith("SELECT uniqExact"):
                rows = [(1,)]
            elif "GROUP BY f.flag_class" in sql:
                rows = [("diversion", 1)]
            elif "AS o," in sql:
                rows = [("HND", "ITM", 1)]
            elif "AS flag_class" in sql:
                rows = [_FLAG_ROW]
            elif sql.startswith("SELECT toString("):
                rows = [_INSTANCE_COLS]

        class _Res:
            result_rows = rows

        return _Res()

    def close(self):
        pass


def test_fetch_wb_flags_falls_back_to_no_tier_variant(livemap, monkeypatch):
    client = _LadderClient(missing=("fct_flight_recon_tier",))
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    out = livemap.wb_store.fetch_flags("", None, None, 50, 0)
    assert out["available"] is True
    assert out["flags"][0]["tier"] == "unknown"
    assert out["flags"][0]["flag_class"] == "diversion"
    assert out["flags"][0]["detail"] == "dest RJNA vs modal RJGG 79/87"
    assert out["classes"] == {"diversion": 1}
    assert out["total"] == 1
    assert livemap.wb.FLAGS_QUERY_NO_TIER in client.sqls


def test_fetch_wb_flags_unavailable_without_flags_mart(livemap, monkeypatch):
    client = _LadderClient(missing=("fct_flight_flags",))
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    out = livemap.wb_store.fetch_flags("", None, None, 50, 0)
    assert out == {"available": False, "flags": [], "classes": {}, "total": 0,
                   "limit": 50, "offset": 0}


def test_fetch_wb_summary_probe_drops_absent_sections(livemap, monkeypatch):
    client = _LadderClient(missing=("fct_flight_flags", "fct_est_settlement"),
                           present=("fct_flight_recon_tier",))
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    out = livemap.wb_store.fetch_summary(datetime.date(2026, 7, 10), datetime.date(2026, 8, 8))
    assert out["flags"] == {"available": False, "flagged": 0, "classes": {}}
    assert out["est"] == {"available": False, "err_p50_km": None, "n": 0, "daily": []}
    assert out["tiers"]["available"] is True
    retried = client.sqls[-1]
    assert "fct_flight_flags" not in retried and "fct_est_settlement" not in retried
    assert "fct_flight_recon_tier" in retried


class _Slice3Client(_LadderClient):
    # _LadderClient's row table knows nothing about the estimates/coverage query shapes — this
    # subclass answers each of them with one plausible row so a fetcher never trips on an empty read.
    def query(self, sql, **_kw):
        for name in self.missing:
            if name in sql:
                raise _FakeUnknownTableError("Code: 60. DB::Exception: (UNKNOWN_TABLE)")
        rows = []
        if "AS first_day, " in sql:
            rows = [("111", 2, 5.0, 9.0, "2026-07-29", "2026-07-29")]
        elif sql.startswith("SELECT day, cfg,"):
            rows = [("2026-07-29", "111", 5.0, 9.0, 2)]
        elif "n_settled" in sql:
            rows = [(4, 1, 1, 3, 3)]
        elif "dimension, value, producer" in sql:
            rows = [("skip", "gap:on_ground_edge", "serving-private", 3)]
        elif "AS tier, " in sql:
            rows = [("2026-07-29", "settled", 2)]
        elif "roundDown" in sql:
            rows = [(0, 1)]
        elif "quantileExact" in sql:
            rows = [("2026-07-29", 0.8, 3)]
        self.sqls.append(sql)

        class _Res:
            result_rows = rows

        return _Res()


def test_fetch_wb_estimates_unavailable_without_the_ledger(livemap, monkeypatch):
    client = _Slice3Client(missing=("fct_est_settlement",))
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    out = livemap.wb_store.fetch_estimates(None, None)
    assert out == livemap.wb.empty_estimates() | {"available": False}
    assert out["available"] is False


def test_fetch_wb_estimates_serves_while_only_the_breakdown_mart_is_missing(livemap, monkeypatch):
    # the two marts deploy independently — a missing breakdown must dim the mix, not the whole view
    client = _Slice3Client(missing=("agg_est_breakdown_daily",))
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    out = livemap.wb_store.fetch_estimates(None, None)
    assert out["available"] is True
    assert out["mix"] == {"available": False, "skip": [], "segment_kind": [], "uncertainty_bin": []}
    assert out["headline"][0]["config_hash"] == "111"
    assert out["outcomes"] == {"settled": 4, "awaiting": 1, "ambiguous": 1}


def test_fetch_wb_coverage_unavailable_without_the_tier_mart(livemap, monkeypatch):
    client = _Slice3Client(missing=("fct_flight_recon_tier",))
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    out = livemap.wb_store.fetch_coverage(None, None)
    assert out == livemap.wb.empty_coverage() | {"available": False}


def test_fetch_wb_coverage_always_serves_all_eight_gap_bins(livemap, monkeypatch):
    client = _Slice3Client(missing=())
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    out = livemap.wb_store.fetch_coverage(None, None)
    # a bin that no flight landed in must still be drawn — otherwise the histogram's shape shifts
    assert [b["ge"] for b in out["gap_bins"]] == list(livemap.wb.GAP_EDGES)
    assert out["gap_bins"][-1]["lt"] is None
    assert [b["n"] for b in out["gap_bins"]] == [1, 0, 0, 0, 0, 0, 0, 0]


def test_estimates_config_hash_serialized_as_string(livemap, monkeypatch):
    # config_hash is UInt64 — 2537707548349448576 loses its low bits as a JS Number
    client = _Slice3Client(missing=())
    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: client)
    out = livemap.wb_store.fetch_estimates(None, None)
    assert isinstance(out["headline"][0]["config_hash"], str)
    assert isinstance(out["daily"][0]["config_hash"], str)
    assert "toString(config_hash)" in livemap.wb.ESTIMATES_HEADLINE_QUERY
    assert "toString(config_hash)" in livemap.wb.ESTIMATES_DAILY_QUERY


@pytest.mark.parametrize("name", ["estimates", "coverage"])
def test_slice3_day_params_parse_and_reach_the_fetcher(livemap, monkeypatch, name):
    seen = {}

    def fake(day_from, day_to):
        seen["days"] = (day_from, day_to)
        return {}

    monkeypatch.setattr(livemap.wb_store, f"fetch_{name}", fake)
    c = TestClient(livemap.app)
    c.get(f"/workbench/{name}?day_from=2026-07-29&day_to=2026-08-08")
    assert seen["days"] == (datetime.date(2026, 7, 29), datetime.date(2026, 8, 8))
    c.get(f"/workbench/{name}?day_from=garbage&day_to=")
    assert seen["days"] == (None, None)   # malformed days degrade to the wide sentinel range


def test_slice3_pools_never_average_per_row_medians(livemap):
    # median-of-medians is banned: the per-row err_p50_km/err_p90_km columns are a filter, never an input
    wb = livemap.wb
    for q in (wb.ESTIMATES_HEADLINE_QUERY, wb.ESTIMATES_DAILY_QUERY):
        assert "arraySort(groupArrayArray(errs_km)) AS pool" in q
        assert "ORDER BY computed_at, estimate_id LIMIT 1 BY input_fingerprint, seg_idx" in q
        assert "avg(" not in q and "quantile" not in q
        # err_p50_km appears only as the dedup fragment's NOT NULL gate, never inside an aggregate
        assert q.count("err_p50_km") == 1
        assert "err_p90_km" not in q


def test_optional_mart_catalogue_covers_every_degraded_table(livemap):
    # the probe query is the module's declared optional-mart set — a mart a fetcher degrades on but
    # the catalogue omits would make a summary-style probe silently mis-answer "which one is gone?"
    wb = livemap.wb
    for name in ("fct_flight_flags", "fct_flight_recon_tier", "fct_est_settlement",
                 "agg_est_breakdown_daily"):
        assert name in wb.OPTIONAL_TABLES
        assert f"'{name}'" in wb.PROBE_TABLES_QUERY


def test_coverage_windows_on_reconciled_jst_never_the_tier_marts_utc_day(livemap):
    # the tier mart's start_day is UTC; every coverage query has to window on the reconciled JST start
    wb = livemap.wb
    for q in (wb.COVERAGE_TIER_DAILY_QUERY, wb.COVERAGE_GAP_HIST_QUERY, wb.COVERAGE_OBSERVED_QUERY):
        assert "toDate(r.start_time, 'Asia/Tokyo') BETWEEN {day_from:Date} AND {day_to:Date}" in q
        assert "start_day" not in q
    assert "if(coalesce(t.tier, '') = '', 'unknown', t.tier)" in wb.COVERAGE_TIER_DAILY_QUERY


def test_flags_pin_lazy_materialization_off(livemap, monkeypatch):
    # same CH 26.5 optimizer defect as the vanilla instances read: the tier LEFT JOIN plus the
    # Nullable(DateTime64) sort key needs both settings or the query raises instead of paging
    seen = []

    class FakeClient:
        def query(self, sql, settings=None, **_kw):
            seen.append((sql, settings))

            class _Res:
                result_rows = [(0,)] if sql.startswith("SELECT count()") else []

            return _Res()

        def close(self):
            pass

    monkeypatch.setattr(livemap.wb_store, "client_factory", lambda: FakeClient())
    livemap.wb_store.fetch_flags("", None, None, 50, 0)
    main = [s for sql, s in seen if sql.startswith("SELECT toString(")]
    assert len(main) == 1
    assert all(s and s.get("query_plan_optimize_lazy_materialization") == 0 for s in main)


# ---- LADD private pin: the workbench never filters, extends the 17-pin family ----

def test_private_instances_ladd_listed_hex_unfiltered(livemap, monkeypatch):
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
    monkeypatch.setattr(livemap.wb_store, "fetch_instances", lambda *_a, **_kw: payload)
    r = TestClient(livemap.app).get("/workbench/instances?hex=abc123")
    assert r.json() == payload   # no LADD filtering exists anywhere on the workbench path


def test_private_search_ladd_listed_airframe_unfiltered(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", {"hex": frozenset({"abc123"}), "callsign": frozenset()})
    payload = {"airlines": [], "services": [],
              "airframes": [{"icao24": "abc123", "registration": "JA123", "typecode": "B738",
                            "n_instances": 3}],
              "airports": []}
    monkeypatch.setattr(livemap.wb_store, "fetch_search", lambda _q, _limit: payload)
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


def test_summary_params_previous_window_is_the_same_span_immediately_before(livemap):
    wb = livemap.wb
    p = wb.summary_params(datetime.date(2026, 7, 10), datetime.date(2026, 8, 8))
    assert p["prev_from"] == datetime.date(2026, 6, 10)   # 30-day span, ends the day before day_from
    assert p["prev_to"] == datetime.date(2026, 7, 9)
    # an unset range has no comparable previous window — every prev_n must come back 0
    q = wb.summary_params(None, None)
    assert (q["day_from"], q["day_to"]) == (datetime.date(1900, 1, 1), datetime.date(2999, 12, 31))
    assert q["prev_from"] == q["prev_to"] == datetime.date(1900, 1, 1)
    # at the sentinel floor the subtraction would underflow — it clamps instead of raising
    f = wb.summary_params(datetime.date(1900, 1, 1), datetime.date(1900, 1, 5))
    assert f["prev_from"] == f["prev_to"] == datetime.date(1900, 1, 1)


def test_shape_summary_reorders_movers_regardless_of_row_arrival(livemap):
    # UNION ALL block interleaving may not preserve the mover arm's inner ORDER BY
    rows = [("mover", "HND-CTS", "", 1804.0, 1760.0),
            ("mover", "GMP-CJU", "", 3072.0, 2734.0),
            ("mover", "CJU-GMP", "", 3072.0, 2763.0)]
    out = livemap.wb.shape_summary(rows, True, True, True)
    assert [m["key"] for m in out["movers"]] == ["CJU-GMP", "GMP-CJU", "HND-CTS"]


# ---- Dockerfile / file layout ----

def test_workbench_files_exist_next_to_app():
    livemap_dir = REPO_ROOT / "livemap"
    assert (livemap_dir / "workbench.py").exists()
    assert (livemap_dir / "routes_workbench.py").exists()
    assert (livemap_dir / "wb_store.py").exists()
    # the store, models and contract are image-baked too — a missing COPY would boot a workbench-less
    # private sidecar (or a /features that cannot answer the handshake)
    copied = {tok for line in (livemap_dir / "Dockerfile").read_text().splitlines() if line.startswith("COPY ")
              for tok in line.split()[1:-1]}
    assert {"wb_store.py", "wb_models.py", "wb_contract.json"} <= copied


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
        for tier in (True, False):
            for asc in (True, False):
                run(wb.instances_query(tier=tier, asc=asc), iparams, qs)
            run(wb.instances_count_query(tier=tier), iparams)
            run(wb.instances_od_breakdown_query(tier=tier), iparams)
        run(wb.instances_query(tier=True, asc=False), _harmless_instances_params(livemap, military=1), qs)

        sparams = wb.search_params("AN")
        sparams["limit"] = 2
        run(wb.SEARCH_AIRLINES_QUERY, sparams)
        run(wb.SEARCH_SERVICES_QUERY, sparams)
        run(wb.SEARCH_AIRFRAMES_QUERY, sparams)
        run(wb.SEARCH_AIRPORTS_QUERY, sparams)

        run(wb.PROBE_TABLES_QUERY, {"db": wb.CH_DB})

        # every has_* combination, so a degraded assembly can't ship a syntactically broken union
        wparams = wb.summary_params(datetime.date(2026, 1, 1), datetime.date(2026, 1, 7))
        for has_flags in (False, True):
            for has_tier in (False, True):
                for has_est in (False, True):
                    run(wb.summary_query(has_flags, has_tier, has_est), wparams)

        tparams = wb.trends_params(datetime.date(2026, 1, 1), datetime.date(2026, 1, 7), 2, 0)
        for dim in ("route", "airline", "airport"):
            run(wb.TRENDS_RANK_QUERY[dim], tparams)
            run(wb.TRENDS_SERIES_QUERY[dim], tparams | {"keys": ["ZZZ-YYY"]})
            run(wb.TRENDS_TOTAL_QUERY[dim], tparams)

        flparams = wb.flags_params("diversion", datetime.date(2026, 1, 1), datetime.date(2026, 1, 7))
        flparams["limit"], flparams["offset"] = 2, 0
        run(wb.FLAGS_QUERY_TIER, flparams, qs)
        run(wb.FLAGS_QUERY_NO_TIER, flparams, qs)
        run(wb.FLAGS_COUNT_QUERY, flparams)
        run(wb.FLAGS_CLASSES_QUERY, flparams)

        # slice 3: both a real window and the wide sentinel range, since the sentinel is what an
        # unset/garbage day binds and it is the shape that scans the whole mart
        for dparams in (wb.estimates_params(datetime.date(2026, 1, 1), datetime.date(2026, 1, 7)),
                        wb.estimates_params(None, None)):
            run(wb.ESTIMATES_HEADLINE_QUERY, dparams)
            run(wb.ESTIMATES_DAILY_QUERY, dparams)
            run(wb.ESTIMATES_MIX_QUERY, dparams)
            run(wb.ESTIMATES_OUTCOMES_QUERY, dparams)
        for cparams in (wb.coverage_params(datetime.date(2026, 1, 1), datetime.date(2026, 1, 7)),
                        wb.coverage_params(None, None)):
            run(wb.COVERAGE_TIER_DAILY_QUERY, cparams)
            run(wb.COVERAGE_GAP_HIST_QUERY, cparams)
            run(wb.COVERAGE_OBSERVED_QUERY, cparams)
    finally:
        client.close()


def test_slice3_query_forms_need_no_optimizer_pins(livemap):
    # measured 2026-08-13 on CH 26.5: every slice-3 form runs clean with BOTH defect-carrying
    # optimizations at their server defaults, so pinning them here would be cargo-cult
    client = _live_ch_client()
    wb = livemap.wb
    settings = {"query_plan_optimize_lazy_materialization": 1, "use_top_k_dynamic_filtering": 1}
    params = wb.coverage_params(datetime.date(2026, 1, 1), datetime.date(2026, 1, 7))
    try:
        for sql in (wb.ESTIMATES_HEADLINE_QUERY, wb.ESTIMATES_DAILY_QUERY, wb.ESTIMATES_MIX_QUERY,
                    wb.ESTIMATES_OUTCOMES_QUERY, wb.COVERAGE_TIER_DAILY_QUERY,
                    wb.COVERAGE_GAP_HIST_QUERY, wb.COVERAGE_OBSERVED_QUERY):
            try:
                client.query(sql, parameters=params, settings=settings)
            except Exception as exc:   # an optional mart may be absent — tolerate only that
                assert livemap._is_unknown_table_error(exc), f"unexpected live-CH failure: {exc}"
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
            rows = client.query(wb.instances_query(tier=False, asc=False), parameters=p,
                                settings=wb.INSTANCES_QUERY_SETTINGS).result_rows
            return {r[0]: r[1] for r in rows}

        on_jst = ids_for(jst_day)
        assert fid in on_jst
        assert on_jst[fid] == jst_day.isoformat()   # the served `day` column is the JST one too
        assert fid not in ids_for(utc_day)
    finally:
        client.close()


# ---- seeded JST value oracle (#146): hand-computed literals on a throwaway CH database ----

# unique per run: a fixed name would let two concurrent sessions drop each other's tables mid-test
_ORACLE_DB = f"wb_oracle_pr2_{uuid.uuid4().hex[:8]}"
_D28 = datetime.date(2026, 7, 28)
_D29 = datetime.date(2026, 7, 29)
_ORACLE_SCHEMA = (
    "CREATE TABLE {db}.fct_flights_reconciled ("
    "flight_id UInt64, icao24 Nullable(String), callsign Nullable(String), "
    "start_time DateTime64(6, 'UTC'), end_time Nullable(DateTime64(6, 'UTC')), "
    "registration Nullable(String), typecode Nullable(String), airline_name Nullable(String), "
    "origin_icao Nullable(String), origin_iata Nullable(String), origin_city Nullable(String), "
    "dest_icao Nullable(String), dest_iata Nullable(String), dest_city Nullable(String)"
    ") ENGINE = MergeTree ORDER BY tuple()",
    "CREATE TABLE {db}.fct_flight_flags ("
    "flight_id Nullable(UInt64), start_day Nullable(Date), flag_class String, detail String"
    ") ENGINE = MergeTree ORDER BY tuple()",
    "CREATE TABLE {db}.fct_flight_recon_tier ("
    "flight_id Nullable(UInt64), tier String, effective_gap_s Nullable(Int64), "
    "n_points Nullable(UInt64), is_military Nullable(UInt8), largest_gap_s Nullable(UInt32), "
    "observed_fraction Nullable(Float64)"
    ") ENGINE = MergeTree ORDER BY tuple()",
    "CREATE TABLE {db}.fct_est_settlement ("
    "estimate_id UUID, seg_idx UInt8, input_fingerprint UInt64, computed_at DateTime64(3, 'UTC'), "
    "settled UInt8, skip_ambiguous UInt8, err_p50_km Nullable(Float64), errs_km Array(Float32), "
    "config_hash UInt64, producer LowCardinality(String), input_provisional UInt8"
    ") ENGINE = MergeTree ORDER BY tuple()",
    "CREATE TABLE {db}.agg_est_breakdown_daily ("
    "day Date, producer LowCardinality(String), dimension String, value String, n UInt64"
    ") ENGINE = MergeTree ORDER BY tuple()",
)
# A starts 23:30 UTC, so its JST day is the NEXT calendar day; both flag rows carry the UTC
# start_day, so anything windowing on f.start_day would put A on 07-28 with B.
_ORACLE_SEED = (
    "INSERT INTO {db}.fct_flights_reconciled VALUES "
    "(1001, 'aaa111', 'ANA1', toDateTime64('2026-07-28 23:30:00', 6, 'UTC'), "
    "toDateTime64('2026-07-29 01:00:00', 6, 'UTC'), 'JA111A', 'B738', 'All Nippon Airways', "
    "'RJTT', 'HND', 'Tokyo', 'RJBB', 'ITM', 'Osaka'), "
    "(1002, 'aaa111', 'ANA2', toDateTime64('2026-07-28 12:00:00', 6, 'UTC'), "
    "toDateTime64('2026-07-28 13:30:00', 6, 'UTC'), 'JA111A', 'B738', 'All Nippon Airways', "
    "'RJCC', 'CTS', 'Sapporo', 'RJTT', 'HND', 'Tokyo')",
    "INSERT INTO {db}.fct_flight_flags VALUES "
    "(1001, toDate('2026-07-28'), 'single_source', 'only adsblol voted'), "
    "(1002, toDate('2026-07-28'), 'single_source', 'only adsblol voted')",
    "INSERT INTO {db}.fct_flight_recon_tier VALUES (1001, 'settled', 12, 400, 0, 12, 0.9)",
)

# ---- seeded slice-3 oracle: its own database, so the estimates/coverage literals below are
# ---- hand-computed from THIS seed and can't be perturbed by the pair the other class needs.
_ORACLE3_DB = f"wb_oracle_pr3_{uuid.uuid4().hex[:8]}"
_D30 = datetime.date(2026, 7, 30)
# 2001/2003/2005/2006 land on JST 07-29 (23:30, 23:59, 23:00 and 15:00 UTC on 07-28); 2002/2004 on
# JST 07-28. 2005 has NO tier row — it is the LEFT JOIN miss that must read 'unknown'.
_ORACLE3_SEED = (
    "INSERT INTO {db}.fct_flights_reconciled VALUES "
    "(2001, 'aaa111', 'ANA1', toDateTime64('2026-07-28 23:30:00', 6, 'UTC'), NULL, "
    "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL), "
    "(2002, 'aaa222', 'ANA2', toDateTime64('2026-07-28 12:00:00', 6, 'UTC'), NULL, "
    "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL), "
    "(2003, 'aaa333', 'ANA3', toDateTime64('2026-07-28 23:59:00', 6, 'UTC'), NULL, "
    "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL), "
    "(2004, 'aaa444', 'ANA4', toDateTime64('2026-07-28 00:30:00', 6, 'UTC'), NULL, "
    "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL), "
    "(2005, 'aaa555', 'ANA5', toDateTime64('2026-07-28 23:00:00', 6, 'UTC'), NULL, "
    "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL), "
    "(2006, 'aaa666', 'ANA6', toDateTime64('2026-07-28 15:00:00', 6, 'UTC'), NULL, "
    "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)",
    # 899 and 900 straddle the 15-minute tier seam, which the fixed edges make an exact boundary
    "INSERT INTO {db}.fct_flight_recon_tier VALUES "
    "(2001, 'settled', 12, 400, 0, 899, 0.80), "
    "(2002, 'estimated', 12, 400, 0, 900, 0.40), "
    "(2003, 'settled', 12, 400, 0, 59, 0.90), "
    "(2004, 'none', NULL, NULL, 0, NULL, NULL), "
    "(2006, 'estimated', 12, 400, 0, 3600, 0.10)",
    # config 111: A+B pool to ten point-errors 1..10; C repeats A's fingerprint LATER, so the
    # dedup must drop it (were it kept, the pool would run to 100 and p90 would read 100).
    "INSERT INTO {db}.fct_est_settlement VALUES "
    "(generateUUIDv4(), 0, 7001, toDateTime64('2026-07-28 23:30:00', 3, 'UTC'), 1, 0, 3.0, "
    "[1, 2, 3, 4, 5], 111, 'serving-private', 0), "
    "(generateUUIDv4(), 0, 7002, toDateTime64('2026-07-28 23:31:00', 3, 'UTC'), 1, 0, 8.0, "
    "[6, 7, 8, 9, 10], 111, 'serving-private', 0), "
    "(generateUUIDv4(), 0, 7001, toDateTime64('2026-07-28 23:32:00', 3, 'UTC'), 1, 0, 100.0, "
    "[100, 100, 100, 100, 100], 111, 'serving-private', 1), "
    "(generateUUIDv4(), 0, 7003, toDateTime64('2026-07-29 23:30:00', 3, 'UTC'), 1, 0, 20.0, "
    "[20, 20, 20, 20], 222, 'serving-public', 0), "
    "(generateUUIDv4(), 0, 7004, toDateTime64('2026-07-28 23:40:00', 3, 'UTC'), 0, 0, NULL, "
    "[], 111, 'serving', 1), "
    "(generateUUIDv4(), 0, 7005, toDateTime64('2026-07-28 23:41:00', 3, 'UTC'), 0, 1, NULL, "
    "[], 111, 'serving', 1)",
    # UTC-day grain, deliberately skewed against the JST series above (the documented seam)
    "INSERT INTO {db}.agg_est_breakdown_daily VALUES "
    "(toDate('2026-07-28'), 'serving-private', 'skip', 'gap:on_ground_edge', 3), "
    "(toDate('2026-07-28'), 'serving-public', 'skip', 'gap:on_ground_edge', 2), "
    "(toDate('2026-07-29'), 'serving-private', 'segment_kind', 'gap', 7), "
    "(toDate('2026-07-29'), 'serving', 'uncertainty_bin', 'dr', 1), "
    "(toDate('2026-07-29'), 'serving-private', 'bogus_dim', 'x', 5)",
)


def _seeded_oracle(db, seed):
    client = _live_ch_client()
    try:
        client.command(f"DROP DATABASE IF EXISTS {db}")
        client.command(f"CREATE DATABASE {db}")
        for stmt in _ORACLE_SCHEMA + seed:
            client.command(stmt.format(db=db))
        yield client
    finally:
        client.command(f"DROP DATABASE IF EXISTS {db}")
        client.close()


def _oracle_wb(monkeypatch, db, mod_name):
    # the schema knob is read at import time, so the env has to be set before exec_module
    monkeypatch.setenv("LIVEMAP_CH_DB", db)
    spec = importlib.util.spec_from_file_location(mod_name, REPO_ROOT / "livemap" / "workbench.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def oracle_ch():
    yield from _seeded_oracle(_ORACLE_DB, _ORACLE_SEED)


@pytest.fixture
def wb_oracle(monkeypatch):
    return _oracle_wb(monkeypatch, _ORACLE_DB, "wb_oracle_pr2")


@pytest.fixture
def oracle3_ch():
    yield from _seeded_oracle(_ORACLE3_DB, _ORACLE3_SEED)


@pytest.fixture
def wb_oracle3(monkeypatch):
    return _oracle_wb(monkeypatch, _ORACLE3_DB, "wb_oracle_pr3")


class TestSeededJstOracle:
    def test_instances_day_window_splits_the_pair_on_jst(self, oracle_ch, wb_oracle):
        def ids_for(day):
            p = wb_oracle.instances_params("", "", "", "", "", "", "", 0, day, day)
            p["limit"], p["offset"] = 50, 0
            return [r[0] for r in oracle_ch.query(
                wb_oracle.instances_query(tier=False, asc=False), parameters=p,
                settings=wb_oracle.INSTANCES_QUERY_SETTINGS).result_rows]

        assert ids_for(_D29) == ["1001"]
        assert ids_for(_D28) == ["1002"]

    def test_flags_feed_windows_on_jst_start_time_not_flag_start_day(self, oracle_ch, wb_oracle):
        def rows_for(day):
            p = wb_oracle.flags_params("", day, day)
            p["limit"], p["offset"] = 50, 0
            return [(r[0], r[19], r[20]) for r in oracle_ch.query(
                wb_oracle.FLAGS_QUERY_TIER, parameters=p,
                settings=wb_oracle.INSTANCES_QUERY_SETTINGS).result_rows]

        # both flag rows carry start_day 2026-07-28 — this would be empty if the feed used it
        assert rows_for(_D29) == [("1001", "single_source", "only adsblol voted")]
        assert rows_for(_D28) == [("1002", "single_source", "only adsblol voted")]

    def test_summary_daily_and_flagged_are_jst_bucketed(self, oracle_ch, wb_oracle):
        def summary_for(day_from, day_to):
            p = wb_oracle.summary_params(day_from, day_to)
            rows = oracle_ch.query(wb_oracle.summary_query(True, True, True),
                                   parameters=p).result_rows
            return wb_oracle.shape_summary(rows, True, True, True)

        both = summary_for(_D28, _D29)
        assert both["flights"] == 2
        assert both["aircraft"] == 1
        assert both["services"] == 2
        assert both["daily"] == [["2026-07-28", 1], ["2026-07-29", 1]]
        assert both["flags"]["flagged"] == 2
        assert both["flags"]["classes"] == {"single_source": 2}
        assert both["tiers"]["mix"] == {"settled": 1, "unknown": 1}
        assert both["est"] == {"available": True, "err_p50_km": None, "n": 0, "daily": []}
        one = summary_for(_D29, _D29)
        assert one["daily"] == [["2026-07-29", 1]]
        assert one["flags"]["flagged"] == 1
        assert one["tiers"]["daily"] == [["2026-07-29", {"settled": 1}]]

    def test_trends_route_rank_is_jst_bucketed(self, oracle_ch, wb_oracle):
        p = wb_oracle.trends_params(_D29, _D29, 10, 0)
        rows = oracle_ch.query(wb_oracle.TRENDS_RANK_QUERY["route"], parameters=p).result_rows
        assert [(r[0], r[1], r[2]) for r in rows] == [("HND-ITM", 1, 1)]
        p28 = wb_oracle.trends_params(_D28, _D28, 10, 0)
        rows28 = oracle_ch.query(wb_oracle.TRENDS_RANK_QUERY["route"], parameters=p28).result_rows
        assert [(r[0], r[1], r[2]) for r in rows28] == [("CTS-HND", 1, 1)]


# ---- seeded slice-3 oracles: hand-computed pooled percentiles and JST coverage literals ----

def _estimates(ch, wb, day_from, day_to, mix_available=True):
    p = wb.estimates_params(day_from, day_to)
    return wb.shape_estimates(
        ch.query(wb.ESTIMATES_HEADLINE_QUERY, parameters=p).result_rows,
        ch.query(wb.ESTIMATES_DAILY_QUERY, parameters=p).result_rows,
        ch.query(wb.ESTIMATES_MIX_QUERY, parameters=p).result_rows,
        ch.query(wb.ESTIMATES_OUTCOMES_QUERY, parameters=p).result_rows[0],
        mix_available,
    )


def _coverage(ch, wb, day_from, day_to):
    p = wb.coverage_params(day_from, day_to)
    return wb.shape_coverage(
        ch.query(wb.COVERAGE_TIER_DAILY_QUERY, parameters=p).result_rows,
        ch.query(wb.COVERAGE_GAP_HIST_QUERY, parameters=p).result_rows,
        ch.query(wb.COVERAGE_OBSERVED_QUERY, parameters=p).result_rows,
    )


class TestSeededEstimatesOracle:
    def test_pooled_p50_p90_use_ceil_indexing_over_the_deduped_pool(self, oracle3_ch, wb_oracle3):
        # config 111's two surviving rows pool to [1..10]: p50 = pool[ceil(0.5*10)] = pool[5] = 5,
        # p90 = pool[ceil(0.9*10)] = pool[9] = 9. A mean of the two per-row medians would read 5.5.
        out = _estimates(oracle3_ch, wb_oracle3, _D29, _D30)
        by_cfg = {e["config_hash"]: e for e in out["headline"]}
        assert by_cfg["111"]["p50_km"] == 5.0
        assert by_cfg["111"]["p90_km"] == 9.0
        assert by_cfg["111"]["n"] == 2      # the repeated-fingerprint recompute row is NOT counted
        # config 222 pools to [20,20,20,20]: p50 = pool[2], p90 = pool[ceil(3.6)] = pool[4]
        assert (by_cfg["222"]["p50_km"], by_cfg["222"]["p90_km"], by_cfg["222"]["n"]) == (20.0, 20.0, 1)

    def test_duplicate_fingerprint_recompute_is_excluded_by_the_dedup(self, oracle3_ch, wb_oracle3):
        # the third row repeats fingerprint 7001 with errors of 100 km; without LIMIT 1 BY the pool
        # would run to fifteen values and p90 would read 100
        out = _estimates(oracle3_ch, wb_oracle3, _D29, _D30)
        by_cfg = {e["config_hash"]: e for e in out["headline"]}
        assert by_cfg["111"]["p90_km"] != 100.0
        # ...yet the raw logging stream still counts it: four settled rows against two scored inputs
        assert out["outcomes"]["settled"] == 4

    def test_headline_orders_latest_last_seen_first_and_hashes_are_strings(self, oracle3_ch, wb_oracle3):
        out = _estimates(oracle3_ch, wb_oracle3, _D29, _D30)
        assert [e["config_hash"] for e in out["headline"]] == ["222", "111"]
        assert all(isinstance(e["config_hash"], str) for e in out["headline"])
        assert out["headline"][0]["first_day"] == out["headline"][0]["last_day"] == "2026-07-30"
        assert out["headline"][1]["first_day"] == out["headline"][1]["last_day"] == "2026-07-29"

    def test_daily_series_carries_config_hash_so_an_era_change_reads_as_a_break(self, oracle3_ch, wb_oracle3):
        out = _estimates(oracle3_ch, wb_oracle3, _D29, _D30)
        assert out["daily"] == [
            {"day": "2026-07-29", "config_hash": "111", "p50_km": 5.0, "p90_km": 9.0, "n": 2},
            {"day": "2026-07-30", "config_hash": "222", "p50_km": 20.0, "p90_km": 20.0, "n": 1},
        ]

    def test_window_is_jst_on_computed_at(self, oracle3_ch, wb_oracle3):
        # every scored row was written 2026-07-28/29 UTC — a UTC window would find them on 07-28
        one = _estimates(oracle3_ch, wb_oracle3, _D29, _D29)
        assert [e["config_hash"] for e in one["headline"]] == ["111"]
        assert _estimates(oracle3_ch, wb_oracle3, _D28, _D28)["headline"] == []

    def test_outcomes_and_input_split_count_raw_rows(self, oracle3_ch, wb_oracle3):
        out = _estimates(oracle3_ch, wb_oracle3, _D29, _D30)
        assert out["outcomes"] == {"settled": 4, "awaiting": 1, "ambiguous": 1}
        assert out["input_split"] == {"provisional": 3, "settled": 3}

    def test_mix_is_utc_day_grain_the_documented_seam(self, oracle3_ch, wb_oracle3):
        # the breakdown mart's day is UTC, so the SAME window that finds config 111's JST 07-29
        # series picks up the mart's 07-29 UTC rows — not the 07-28 UTC ones that produced it
        one = _estimates(oracle3_ch, wb_oracle3, _D29, _D29)
        assert one["mix"]["skip"] == []
        assert one["mix"]["segment_kind"] == [{"value": "gap", "producer": "serving-private", "n": 7}]
        assert one["mix"]["uncertainty_bin"] == [{"value": "dr", "producer": "serving", "n": 1}]
        both = _estimates(oracle3_ch, wb_oracle3, _D28, _D29)
        # producer is a facet, not a rollup: the two skip producers stay separate rows, n DESC
        assert both["mix"]["skip"] == [
            {"value": "gap:on_ground_edge", "producer": "serving-private", "n": 3},
            {"value": "gap:on_ground_edge", "producer": "serving-public", "n": 2},
        ]
        assert "bogus_dim" not in both["mix"]   # a dimension the view has no panel for is dropped

    def test_mix_absent_leaves_the_rest_of_the_view_intact(self, oracle3_ch, wb_oracle3):
        out = _estimates(oracle3_ch, wb_oracle3, _D29, _D30, mix_available=False)
        assert out["available"] is True
        assert out["mix"]["available"] is False
        assert out["headline"][0]["config_hash"] == "222"


class TestSeededCoverageOracle:
    def test_tier_mix_buckets_on_jst_and_names_the_join_miss_unknown(self, oracle3_ch, wb_oracle3):
        out = _coverage(oracle3_ch, wb_oracle3, _D28, _D29)
        # 2001/2003/2005/2006 start at 23:30, 23:59, 23:00 and 15:00 UTC on 07-28 -> JST 07-29
        assert out["tier_daily"] == [
            ["2026-07-28", {"estimated": 1, "none": 1}],
            ["2026-07-29", {"estimated": 1, "settled": 2, "unknown": 1}],
        ]
        # 2005 has no tier row at all — the LEFT JOIN miss must read 'unknown', never a real tier
        one = _coverage(oracle3_ch, wb_oracle3, _D29, _D29)
        assert one["tier_daily"] == [["2026-07-29", {"estimated": 1, "settled": 2, "unknown": 1}]]

    def test_gap_histogram_edges_are_exact_and_every_bin_is_served(self, oracle3_ch, wb_oracle3):
        out = _coverage(oracle3_ch, wb_oracle3, _D28, _D29)
        assert [b["ge"] for b in out["gap_bins"]] == [0, 60, 300, 900, 3600, 10800, 21600, 43200]
        assert [b["lt"] for b in out["gap_bins"]] == [60, 300, 900, 3600, 10800, 21600, 43200, None]
        # 59 -> bin 0, 899 -> bin 300, 900 -> bin 900 (the 15-minute tier seam is an EXACT edge,
        # so the boundary value lands above it), 3600 -> bin 3600; the NULL-gap row is excluded
        assert [b["n"] for b in out["gap_bins"]] == [1, 0, 1, 1, 1, 0, 0, 0]

    def test_observed_fraction_is_a_per_day_median_over_non_null_values(self, oracle3_ch, wb_oracle3):
        out = _coverage(oracle3_ch, wb_oracle3, _D28, _D29)
        # JST 07-29 holds 0.80, 0.90, 0.10 (2005 contributes no tier row) -> median 0.80, n 3;
        # JST 07-28 holds only 0.40 (2004's NULL is excluded, not read as a zero) -> 0.40, n 1
        assert out["observed"] == [
            {"day": "2026-07-28", "median": 0.4, "n": 1},
            {"day": "2026-07-29", "median": 0.8, "n": 3},
        ]
