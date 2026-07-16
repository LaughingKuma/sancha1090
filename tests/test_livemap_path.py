import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def livemap():
    spec = importlib.util.spec_from_file_location("livemap_app", REPO_ROOT / "livemap" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def allow_path_auth(livemap, monkeypatch):
    original = livemap._fetch_path_auth
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(livemap, "_fetch_path_auth", lambda _fid: ("abc123", "ANA1", False))
    return original


def test_path_query_shape(livemap):
    q = livemap.PATH_QUERY
    # schema comes from LIVEMAP_CH_DB so the env knob really governs the query, not just the client
    assert f"{livemap.CH_DB}.fct_flight_path" in q
    assert "{fid:UInt64}" in q                                  # parameterized, never interpolated
    assert f"{livemap.CH_DB}.fct_flights_reconciled" in q       # LADD gate joins the reconciled mart
    assert "is_ladd = 0" in q                                   # window-aware suppression rides the subquery
    assert "ORDER BY ts" in q


def test_path_auth_query_shape(livemap):
    q = livemap.PATH_AUTH_QUERY
    assert f"{livemap.CH_DB}.fct_flights_reconciled" in q
    assert "{fid:UInt64}" in q
    assert "icao24" in q and "callsign" in q and "is_ladd" in q


def test_valid_flight_id_accepts_uint64_digits(livemap):
    assert livemap._valid_flight_id("0")
    assert livemap._valid_flight_id("12345678901234567890")     # cityHash64 > 2**63
    assert livemap._valid_flight_id(str(2**64 - 1))             # UInt64 max fits


def test_valid_flight_id_rejects_nondigit_and_overflow(livemap):
    assert not livemap._valid_flight_id("")
    assert not livemap._valid_flight_id("abc")
    assert not livemap._valid_flight_id("12a")
    assert not livemap._valid_flight_id("-1")
    assert not livemap._valid_flight_id("1.0")
    assert not livemap._valid_flight_id("²³")                    # unicode digits are not ASCII
    assert not livemap._valid_flight_id(str(2**64))             # one past UInt64 max


def test_valid_flight_id_rejects_huge_without_raising(livemap):
    # the len cap must reject before int() — a 5000-digit str would ValueError past CPython's 4300-digit limit
    assert livemap._valid_flight_id("9" * 5000) is False


def test_path_invalid_id_returns_empty_without_query(livemap, monkeypatch):
    # a bad id must short-circuit to empty before any CH call — never a 404/500
    def boom(_fid):
        raise AssertionError("_fetch_path must not run for an invalid id")

    monkeypatch.setattr(livemap, "_fetch_path", boom)
    r = TestClient(livemap.app).get("/path/notanumber")
    assert r.status_code == 200
    assert r.json() == {"flight_id": "notanumber", "points": []}


def test_path_huge_digit_id_returns_empty_200(livemap, monkeypatch):
    # _valid_flight_id runs OUTSIDE the endpoint try/except, so a 5000-digit id (which int() would ValueError on)
    # must be rejected by the length guard as empty 200 — never a 500, and never a CH call.
    def boom(_fid):
        raise AssertionError("_fetch_path must not run for an oversized id")

    monkeypatch.setattr(livemap, "_fetch_path", boom)
    big = "9" * 5000
    r = TestClient(livemap.app).get(f"/path/{big}")
    assert r.status_code == 200
    assert r.json() == {"flight_id": big, "points": []}


def test_fetch_path_shapes_points(livemap, monkeypatch):
    # CH projects (ts_epoch, lon, lat, alt_ft, source); the payload reorders to [lon, lat, ts, alt, source]
    class FakeRes:
        result_rows = [(1765500000, 139.7, 35.6, 38000.0, "adsb"),
                       (1765500002, 139.8, 35.7, None, "adsblol")]

    class FakeClient:
        def query(self, _sql, parameters=None):
            assert parameters == {"fid": 42}     # bound as an int, not the raw string
            return FakeRes()

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: FakeClient())
    out = livemap._fetch_path("42")
    assert out == [
        [139.7, 35.6, 1765500000, 38000.0, "adsb"],
        [139.8, 35.7, 1765500002, None, "adsblol"],
    ]


def test_fetch_path_auth_shapes_identity(livemap, monkeypatch, allow_path_auth):
    class FakeRes:
        result_rows = [("abc123", "ANA1 ", 1)]

    class FakeClient:
        def query(self, sql, parameters=None):
            assert sql == livemap.PATH_AUTH_QUERY
            assert parameters == {"fid": 42}
            return FakeRes()

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: FakeClient())
    assert allow_path_auth("42") == ("abc123", "ANA1 ", True)


def test_path_points_passthrough(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})
    pts = [[139.7, 35.6, 1765500000, 38000.0, "adsb"]]
    monkeypatch.setattr(livemap, "_fetch_path", lambda _fid: pts)
    j = TestClient(livemap.app).get("/path/42").json()
    assert j == {"flight_id": "42", "points": pts}


def test_path_ladd_suppressed_returns_empty(livemap, monkeypatch):
    # Current open-list authorization runs before both the trajectory query and any geometry-cache hit.
    monkeypatch.setattr(livemap, "_path_cache", {})
    monkeypatch.setattr(
        livemap,
        "_ladd_suppress",
        {"hex": frozenset({"abc123"}), "callsign": frozenset()},
    )
    monkeypatch.setattr(
        livemap,
        "_fetch_path",
        lambda _fid: (_ for _ in ()).throw(AssertionError("suppressed path must not be fetched")),
    )
    r = TestClient(livemap.app).get("/path/42")
    assert r.status_code == 200
    assert r.json() == {"flight_id": "42", "points": []}


def test_path_mart_ladd_flag_suppresses_cached_geometry(livemap, monkeypatch):
    pts = [[139.7, 35.6, 1765500000, 38000.0, "adsb"]]
    monkeypatch.setattr(livemap, "_path_cache", {"42": (float("inf"), pts)})
    monkeypatch.setattr(livemap, "_fetch_path_auth", lambda _fid: ("abc123", "ANA1", True))
    assert TestClient(livemap.app).get("/path/42").json()["points"] == []


def test_path_unloaded_ladd_state_fails_closed_before_query(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", None)
    monkeypatch.setattr(
        livemap,
        "_fetch_path_auth",
        lambda _fid: (_ for _ in ()).throw(AssertionError("auth must not run before LADD loads")),
    )
    assert TestClient(livemap.app).get("/path/42").json()["points"] == []


def test_path_missing_auth_row_suppresses_cached_geometry(livemap, monkeypatch):
    pts = [[139.7, 35.6, 1765500000, 38000.0, "adsb"]]
    monkeypatch.setattr(livemap, "_path_cache", {"42": (float("inf"), pts)})
    monkeypatch.setattr(livemap, "_fetch_path_auth", lambda _fid: None)
    assert TestClient(livemap.app).get("/path/42").json()["points"] == []


def test_path_auth_failure_suppresses_cached_geometry(livemap, monkeypatch):
    pts = [[139.7, 35.6, 1765500000, 38000.0, "adsb"]]
    monkeypatch.setattr(livemap, "_path_cache", {"42": (float("inf"), pts)})
    monkeypatch.setattr(
        livemap,
        "_fetch_path_auth",
        lambda _fid: (_ for _ in ()).throw(RuntimeError("ch down")),
    )
    assert TestClient(livemap.app).get("/path/42").json()["points"] == []


def test_path_ch_failure_returns_empty_200(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})

    def boom(_fid):
        raise RuntimeError("ch down")

    monkeypatch.setattr(livemap, "_fetch_path", boom)
    r = TestClient(livemap.app).get("/path/42")
    assert r.status_code == 200
    assert r.json() == {"flight_id": "42", "points": []}


def test_path_missing_table_returns_empty_200(livemap, monkeypatch):
    # pre-first-build: fct_flight_path doesn't exist yet — the broad catch reads it as no-path, not an outage
    monkeypatch.setattr(livemap, "_path_cache", {})

    def missing(_fid):
        raise RuntimeError("Code: 60. DB::Exception: Table gold_ch.fct_flight_path doesn't exist")

    monkeypatch.setattr(livemap, "_fetch_path", missing)
    r = TestClient(livemap.app).get("/path/42")
    assert r.status_code == 200
    assert r.json() == {"flight_id": "42", "points": []}


def test_path_cached_after_first_call(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})
    calls = {"path": 0, "auth": 0}

    def once(_fid):
        calls["path"] += 1
        return []

    def authorize(_fid):
        calls["auth"] += 1
        return "abc123", "ANA1", False

    monkeypatch.setattr(livemap, "_fetch_path", once)
    monkeypatch.setattr(livemap, "_fetch_path_auth", authorize)
    c = TestClient(livemap.app)
    c.get("/path/42")
    c.get("/path/42")
    assert calls == {"path": 1, "auth": 2}  # cached geometry, fresh authorization on every request


def test_path_cache_bounded(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})
    monkeypatch.setattr(livemap, "PATH_CACHE_MAX", 3)
    monkeypatch.setattr(livemap, "_fetch_path", lambda _fid: [])
    c = TestClient(livemap.app)
    for i in range(10):
        c.get(f"/path/{i}")
    # unexpired entries can't ride past the cap — a many-flight sweep must not grow the dict unboundedly
    assert len(livemap._path_cache) <= 3


def test_path_zero_cap_disables_cache_but_serves_points(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})
    monkeypatch.setattr(livemap, "PATH_CACHE_MAX", 0)
    pts = [[139.7, 35.6, 1765500000, 38000.0, "adsb"]]
    monkeypatch.setattr(livemap, "_fetch_path", lambda _fid: pts)
    j = TestClient(livemap.app).get("/path/42").json()
    assert j["points"] == pts          # eviction must never clobber a successful fetch
    assert livemap._path_cache == {}   # cap 0 = cache disabled, loop still terminates


def test_path_query_runs_against_live_ch(livemap, ch_cur):
    # skip until the mart exists (pre-first-build); ch_cur itself skips when CH is unreachable
    ch_cur.execute(f"EXISTS {livemap.CH_DB}.fct_flight_path")
    if not ch_cur.fetchall()[0][0]:
        pytest.skip("fct_flight_path not built yet")
    ch_cur.execute(f"SELECT flight_id FROM {livemap.CH_DB}.fct_flight_path LIMIT 1")
    rows = ch_cur.fetchall()
    if not rows:
        pytest.skip("fct_flight_path is empty")
    ch_cur.execute(livemap.PATH_QUERY, {"fid": int(rows[0][0])})
    pts = ch_cur.fetchall()
    tss = [r[0] for r in pts]                  # ts_epoch is the 1st projected column
    assert tss == sorted(tss)                  # ascending (the ORDER BY ts holds)
    ch_cur.execute(livemap.PATH_AUTH_QUERY, {"fid": int(rows[0][0])})
    assert len(ch_cur.fetchall()) == 1
