import datetime
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
    monkeypatch.setattr(
        livemap, "_fetch_path_auth",
        lambda _fid: ("abc123", "ANA1", False, 1765500000, 1765503600, datetime.date(2026, 6, 1)),
    )
    # far-future head classifies every flight historical, preserving today's settled-arm semantics
    monkeypatch.setattr(livemap, "_path_head", {"expiry": float("inf"), "head": datetime.date(2100, 1, 1)})
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
    assert "toUnixTimestamp(start_time)" in q and "toUnixTimestamp(end_time)" in q
    assert "toDate(start_time)" in q


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
        raise AssertionError("_fetch_path_rich must not run for an invalid id")

    monkeypatch.setattr(livemap, "_fetch_path_rich", boom)
    r = TestClient(livemap.app).get("/path/notanumber")
    assert r.status_code == 200
    assert r.json() == {"flight_id": "notanumber", "points": [], "provisional": False}


def test_path_huge_digit_id_returns_empty_200(livemap, monkeypatch):
    # _valid_flight_id runs OUTSIDE the endpoint try/except, so a 5000-digit id (which int() would ValueError on)
    # must be rejected by the length guard as empty 200 — never a 500, and never a CH call.
    def boom(_fid):
        raise AssertionError("_fetch_path_rich must not run for an oversized id")

    monkeypatch.setattr(livemap, "_fetch_path_rich", boom)
    big = "9" * 5000
    r = TestClient(livemap.app).get(f"/path/{big}")
    assert r.status_code == 200
    assert r.json() == {"flight_id": big, "points": [], "provisional": False}


def test_fetch_path_rich_shapes_points(livemap, monkeypatch):
    # Motion fields must survive the warehouse read while the established wire remains projected from it.
    class FakeRes:
        result_rows = [
            (1765500000, 35.6, 139.7, 38000.0, 0, 450.0, 90.0, "adsb"),
            (1765500002, 35.7, 139.8, None, 1, None, None, "adsblol"),
        ]

    class FakeClient:
        def query(self, _sql, parameters=None):
            assert parameters == {"fid": 42}     # bound as an int, not the raw string
            return FakeRes()

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: FakeClient())
    rich = livemap._fetch_path_rich("42")
    assert rich == [
        (1765500000, 35.6, 139.7, 38000.0, 0, 450.0, 90.0, "adsb"),
        (1765500002, 35.7, 139.8, None, 1, None, None, "adsblol"),
    ]
    assert livemap._lean_points(rich) == [
        [139.7, 35.6, 1765500000, 38000.0, "adsb"],
        [139.8, 35.7, 1765500002, None, "adsblol"],
    ]


def test_fetch_path_auth_shapes_identity(livemap, monkeypatch, allow_path_auth):
    class FakeRes:
        result_rows = [("abc123", "ANA1 ", 1, 1765500000, 1765503600, datetime.date(2026, 6, 1))]

    class FakeClient:
        def query(self, sql, parameters=None):
            assert sql == livemap.PATH_AUTH_QUERY
            assert parameters == {"fid": 42}
            return FakeRes()

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: FakeClient())
    assert allow_path_auth("42") == ("abc123", "ANA1 ", True, 1765500000, 1765503600, datetime.date(2026, 6, 1))


def test_path_points_passthrough(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})
    rich = [(1765500000, 35.6, 139.7, 38000.0, 0, 450.0, 90.0, "adsb")]
    pts = [[139.7, 35.6, 1765500000, 38000.0, "adsb"]]
    monkeypatch.setattr(livemap, "_fetch_path_rich", lambda _fid: rich)
    j = TestClient(livemap.app).get("/path/42").json()
    assert j == {"flight_id": "42", "points": pts, "provisional": False}


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
        "_fetch_path_rich",
        lambda _fid: (_ for _ in ()).throw(AssertionError("suppressed path must not be fetched")),
    )
    r = TestClient(livemap.app).get("/path/42")
    assert r.status_code == 200
    assert r.json() == {"flight_id": "42", "points": [], "provisional": False}


def test_path_mart_ladd_flag_suppresses_cached_geometry(livemap, monkeypatch):
    rich = [(1765500000, 35.6, 139.7, 38000.0, 0, 450.0, 90.0, "adsb")]
    monkeypatch.setattr(livemap, "_path_cache", {42: (float("inf"), rich, 123.0)})
    monkeypatch.setattr(
        livemap, "_fetch_path_auth",
        lambda _fid: ("abc123", "ANA1", True, 1765500000, 1765503600, datetime.date(2026, 6, 1)),
    )
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
    rich = [(1765500000, 35.6, 139.7, 38000.0, 0, 450.0, 90.0, "adsb")]
    monkeypatch.setattr(livemap, "_path_cache", {42: (float("inf"), rich, 123.0)})
    monkeypatch.setattr(livemap, "_fetch_path_auth", lambda _fid: None)
    assert TestClient(livemap.app).get("/path/42").json()["points"] == []


def test_path_auth_failure_suppresses_cached_geometry(livemap, monkeypatch):
    rich = [(1765500000, 35.6, 139.7, 38000.0, 0, 450.0, 90.0, "adsb")]
    monkeypatch.setattr(livemap, "_path_cache", {42: (float("inf"), rich, 123.0)})
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

    monkeypatch.setattr(livemap, "_fetch_path_rich", boom)
    r = TestClient(livemap.app).get("/path/42")
    assert r.status_code == 200
    assert r.json() == {"flight_id": "42", "points": [], "provisional": False}


def test_path_missing_table_returns_empty_200(livemap, monkeypatch):
    # pre-first-build: fct_flight_path doesn't exist yet — the broad catch reads it as no-path, not an outage
    monkeypatch.setattr(livemap, "_path_cache", {})

    def missing(_fid):
        raise RuntimeError("Code: 60. DB::Exception: Table gold_ch.fct_flight_path doesn't exist")

    monkeypatch.setattr(livemap, "_fetch_path_rich", missing)
    r = TestClient(livemap.app).get("/path/42")
    assert r.status_code == 200
    assert r.json() == {"flight_id": "42", "points": [], "provisional": False}


def test_path_cached_after_first_call(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})
    calls = {"path": 0, "auth": 0}

    def once(_fid):
        calls["path"] += 1
        return []

    def authorize(_fid):
        calls["auth"] += 1
        return "abc123", "ANA1", False, 1765500000, 1765503600, datetime.date(2026, 6, 1)

    monkeypatch.setattr(livemap, "_fetch_path_rich", once)
    monkeypatch.setattr(livemap, "_fetch_path_auth", authorize)
    c = TestClient(livemap.app)
    c.get("/path/42")
    c.get("/path/42")
    assert calls == {"path": 1, "auth": 2}  # cached geometry, fresh authorization on every request


def test_path_cache_bounded(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})
    monkeypatch.setattr(livemap, "PATH_CACHE_MAX", 3)
    monkeypatch.setattr(livemap, "_fetch_path_rich", lambda _fid: [])
    c = TestClient(livemap.app)
    for i in range(10):
        c.get(f"/path/{i}")
    # unexpired entries can't ride past the cap — a many-flight sweep must not grow the dict unboundedly
    assert len(livemap._path_cache) <= 3


def test_path_zero_cap_disables_cache_but_serves_points(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})
    monkeypatch.setattr(livemap, "PATH_CACHE_MAX", 0)
    rich = [(1765500000, 35.6, 139.7, 38000.0, 0, 450.0, 90.0, "adsb")]
    pts = [[139.7, 35.6, 1765500000, 38000.0, "adsb"]]
    monkeypatch.setattr(livemap, "_fetch_path_rich", lambda _fid: rich)
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


def test_path_no_store_on_every_branch(livemap, monkeypatch):
    # tunnel checks can't prove early returns — every branch must assert no-store itself
    monkeypatch.setattr(livemap, "_path_cache", {})
    c = TestClient(livemap.app)
    assert c.get("/path/notanumber").headers["cache-control"] == "no-store"          # malformed
    rich = [(1765500000, 35.6, 139.7, 38000.0, 0, 450.0, 90.0, "adsb")]
    monkeypatch.setattr(livemap, "_fetch_path_rich", lambda _fid: rich)
    assert c.get("/path/42").headers["cache-control"] == "no-store"                  # settled
    monkeypatch.setattr(
        livemap, "_fetch_path_rich", lambda _fid: (_ for _ in ()).throw(RuntimeError("down"))
    )
    assert c.get("/path/43").headers["cache-control"] == "no-store"                  # CH error
    monkeypatch.setattr(livemap, "_fetch_path_auth", lambda _fid: None)
    assert c.get("/path/44").headers["cache-control"] == "no-store"                  # unknown
    monkeypatch.setattr(livemap, "_ladd_suppress", None)
    assert c.get("/path/45").headers["cache-control"] == "no-store"                  # fail-closed


def test_path_canonical_cache_key(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {})
    calls = {"n": 0}
    def once(_fid):
        calls["n"] += 1
        return [(1765500000, 35.6, 139.7, 38000.0, 0, 450.0, 90.0, "adsb")]
    monkeypatch.setattr(livemap, "_fetch_path_rich", once)
    c = TestClient(livemap.app)
    c.get("/path/42")
    c.get("/path/042")               # leading-zero alias must hit the same settled entry
    assert calls["n"] == 1
    assert list(livemap._path_cache) == [42]


def test_rich_loader_settled_points_carry_motion_fields(livemap, monkeypatch):
    import asyncio
    rich = [(1765500000, 35.0, 139.0, 35000.0, 0, 450.0, 90.0, "adsb")]
    monkeypatch.setattr(livemap, "_fetch_path_rich", lambda _fid: rich)
    got = asyncio.run(livemap._load_path_input("42"))
    assert got["status"] == "settled"
    assert got["points"] == rich
    assert got["auth"][0] == "abc123"


def test_rich_loader_denied_on_suppress_none(livemap, monkeypatch):
    import asyncio
    monkeypatch.setattr(livemap, "_ladd_suppress", None)
    assert asyncio.run(livemap._load_path_input("42"))["status"] == "denied"


def test_lean_projection_is_the_frozen_wire(livemap):
    rich = [(100, 35.0, 139.0, None, 0, None, None, "opensky")]
    assert livemap._lean_points(rich) == [[139.0, 35.0, 100, None, "opensky"]]


def test_settled_as_of_is_sampled_after_the_read(livemap, monkeypatch):
    # §7: input_as_of = read-COMPLETION time; a ticking clock exposes a branch-entry sample as too early
    import asyncio
    import itertools
    ticks = itertools.count(1000.0, 1.0)
    monkeypatch.setattr(livemap.time, "time", lambda: next(ticks))
    rich = [(1765500000, 35.0, 139.0, 35000.0, 0, 450.0, 90.0, "adsb")]

    def slow_fetch(_fid):
        next(ticks)  # the read itself consumes a tick
        return rich

    monkeypatch.setattr(livemap, "_fetch_path_rich", slow_fetch)
    monkeypatch.setattr(livemap, "_path_cache", {})
    got = asyncio.run(livemap._load_path_input("42"))
    assert got["status"] == "settled"
    fetch_entry_now = 1000.0
    assert got["as_of"] > fetch_entry_now + 1.0   # later than branch entry AND the read's own tick


def test_cache_hit_preserves_original_read_time(livemap, monkeypatch):
    import asyncio
    rich = [(1765500000, 35.0, 139.0, 35000.0, 0, 450.0, 90.0, "adsb")]
    monkeypatch.setattr(livemap, "_fetch_path_rich", lambda _fid: rich)
    first = asyncio.run(livemap._load_path_input("42"))
    monkeypatch.setattr(
        livemap,
        "_fetch_path_rich",
        lambda _fid: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    second = asyncio.run(livemap._load_path_input("42"))
    assert second["points"] == rich
    assert second["as_of"] == first["as_of"]   # §7: input_as_of is the READ time, hits included
