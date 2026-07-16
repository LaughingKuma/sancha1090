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


def test_flights_query_reads_reconciled(livemap):
    q = livemap.FLIGHTS_QUERY
    # schema comes from LIVEMAP_CH_DB so the env knob really governs the query, not just the client
    assert f"{livemap.CH_DB}.fct_flights_reconciled" in q
    assert "UNION ALL" not in q          # single clean source now, no read-time union
    assert "fct_flight_legs" not in q
    assert "ORDER BY ts DESC" in q
    assert "LIMIT 10" in q
    assert "{hex:String}" in q           # parameterized, never interpolated
    assert "coalesce(origin_iata, origin_icao)" in q  # ICAO-only airports still surface a code, not null
    assert "toString(flight_id)" in q    # cityHash64 UInt64 → decimal string (JS Number can't hold it)


def test_fetch_flights_shapes_rows(livemap, monkeypatch):
    ts = datetime.datetime(2026, 6, 29, 8, 55, 59)  # naive, matching CH driver output
    fid = "12345678901234567890"  # cityHash64 > 2**63 — must survive as a string, never a JS-lossy number

    class FakeRes:
        result_rows = [("reconciled", ts, "RJTT", "Tokyo Haneda", None, None, "ANA265 ", fid)]

    class FakeClient:
        def query(self, _sql, parameters=None):
            assert parameters == {"hex": "abc123"}   # lowercased + bound
            return FakeRes()

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: FakeClient())
    out = livemap._fetch_flights("ABC123")
    assert out == [{
        "src": "reconciled",
        "ts": ts.replace(tzinfo=datetime.timezone.utc).timestamp(),  # 1782723359.0
        "origin": {"code": "RJTT", "name": "Tokyo Haneda"},
        "dest": {"code": None, "name": None},
        "callsign": "ANA265",
        "flight_id": fid,
    }]
    assert isinstance(out[0]["flight_id"], str)   # serialized as a string, never coerced to int


def test_flights_passthrough(livemap, monkeypatch):
    # loaded (empty) suppression so passthrough is exercised, not the None-state fail-close
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(livemap, "_flights_cache", {})
    row = {"src": "opensky", "ts": 1.0,
           "origin": {"code": "HND", "name": "Tokyo"},
           "dest": {"code": "HKG", "name": "Hong Kong"}, "callsign": "ANA1"}
    monkeypatch.setattr(livemap, "_fetch_flights", lambda _h: [row])
    j = TestClient(livemap.app).get("/flights/ABC123").json()
    assert j == {"hex": "ABC123", "flights": [row]}


def test_flights_ch_failure_returns_empty_200(livemap, monkeypatch):
    # loaded state so this reaches _fetch_flights and exercises the CH-down path, not the None-state fail-close
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(livemap, "_flights_cache", {})

    def boom(_h):
        raise RuntimeError("ch down")

    monkeypatch.setattr(livemap, "_fetch_flights", boom)
    r = TestClient(livemap.app).get("/flights/abc123")
    assert r.status_code == 200
    assert r.json() == {"hex": "abc123", "flights": []}


def test_flights_cached_after_first_call(livemap, monkeypatch):
    # loaded state so the request reaches the fetch/cache path, not the None-state fail-close
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(livemap, "_flights_cache", {})
    calls = {"n": 0}

    def once(_h):
        calls["n"] += 1
        return []

    monkeypatch.setattr(livemap, "_fetch_flights", once)
    c = TestClient(livemap.app)
    c.get("/flights/abc123")
    c.get("/flights/abc123")
    assert calls["n"] == 1   # second request served from cache


def test_flights_query_runs_against_live_ch(livemap, ch_cur):
    # busiest airframe → guaranteed history; ch_cur skips when CH is unreachable
    ch_cur.execute("SELECT icao24 FROM gold_ch.fact_flights GROUP BY icao24 ORDER BY count() DESC LIMIT 1")
    hex_ = ch_cur.fetchall()[0][0]
    ch_cur.execute(livemap.FLIGHTS_QUERY, {"hex": hex_})
    rows = ch_cur.fetchall()
    assert len(rows) <= 10
    tss = [r[1] for r in rows]                 # ts is the 2nd projected column
    assert tss == sorted(tss, reverse=True)    # newest-first (the wrapped ORDER BY works)
    # the filter admits ICAO-only rows, so the coalesce must surface a code on the side that has one
    assert all(r[2] is not None or r[4] is not None for r in rows)


def test_flights_cache_bounded(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(livemap, "_flights_cache", {})
    monkeypatch.setattr(livemap, "FLIGHTS_CACHE_MAX", 3)
    monkeypatch.setattr(livemap, "_fetch_flights", lambda _h: [])
    c = TestClient(livemap.app)
    for i in range(10):
        c.get(f"/flights/hex{i:03d}")
    # unexpired entries can't ride past the cap — a many-hex sweep must not grow the dict unboundedly
    assert len(livemap._flights_cache) <= 3


def test_flights_zero_cap_disables_cache_but_serves_rows(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(livemap, "_flights_cache", {})
    monkeypatch.setattr(livemap, "FLIGHTS_CACHE_MAX", 0)
    row = {"src": "opensky", "ts": 1.0, "origin": {"code": "HND", "name": None},
           "dest": {"code": None, "name": None}, "callsign": None}
    monkeypatch.setattr(livemap, "_fetch_flights", lambda _h: [row])
    j = TestClient(livemap.app).get("/flights/abc123").json()
    assert j["flights"] == [row]          # eviction must never clobber a successful fetch
    assert livemap._flights_cache == {}   # cap 0 = cache disabled, loop still terminates


def test_flights_none_state_fails_closed(livemap, monkeypatch):
    # suppression never loaded → fail closed for every hex (mirrors /track), never reaching CH
    monkeypatch.setattr(livemap, "_ladd_suppress", None)
    monkeypatch.setattr(livemap, "_flights_cache", {})

    def boom(_h):
        raise AssertionError("None-state /flights must not reach CH")

    monkeypatch.setattr(livemap, "_fetch_flights", boom)
    r = TestClient(livemap.app).get("/flights/ABC123")
    assert r.status_code == 200
    assert r.json() == {"hex": "ABC123", "flights": []}


def test_flights_listed_hex_empty_even_when_cache_warm(livemap, monkeypatch):
    # a newly-listed hex yields empty even if the CH cache already holds its history — the live gate runs
    # before the cache read, and empty is indistinguishable from no-history (no privacy oracle).
    row = {"src": "reconciled", "ts": 1.0, "origin": {"code": "HND", "name": "Tokyo"},
           "dest": {"code": "HKG", "name": "Hong Kong"}, "callsign": "ANA1", "flight_id": "1"}
    monkeypatch.setattr(livemap, "_flights_cache", {"abc123": (float("inf"), [row])})
    monkeypatch.setattr(livemap, "_ladd_suppress", {"hex": frozenset({"abc123"}), "callsign": frozenset()})
    monkeypatch.setattr(
        livemap, "_fetch_flights",
        lambda _h: (_ for _ in ()).throw(AssertionError("a listed hex must not reach CH")),
    )
    r = TestClient(livemap.app).get("/flights/ABC123")
    assert r.status_code == 200
    assert r.json() == {"hex": "ABC123", "flights": []}


def test_flights_listed_callsign_row_dropped_hex_clean(livemap, monkeypatch):
    # per-row callsign belt: the requested hex isn't listed, but one returned flight broadcast a currently-listed
    # callsign — drop only that row (trim+upper before match), keep the clean sibling.
    monkeypatch.setattr(livemap, "_flights_cache", {})
    monkeypatch.setattr(livemap, "_ladd_suppress",
                        {"hex": frozenset(), "callsign": frozenset({"SECRET1"})})
    listed = {"src": "reconciled", "ts": 2.0, "origin": {"code": "HND", "name": "Tokyo"},
              "dest": {"code": "HKG", "name": "Hong Kong"}, "callsign": "SECRET1 ", "flight_id": "2"}
    clean = {"src": "reconciled", "ts": 1.0, "origin": {"code": "HND", "name": "Tokyo"},
             "dest": {"code": "ITM", "name": "Osaka"}, "callsign": "ANA1", "flight_id": "1"}
    monkeypatch.setattr(livemap, "_fetch_flights", lambda _h: [listed, clean])
    j = TestClient(livemap.app).get("/flights/abc123").json()
    assert j == {"hex": "abc123", "flights": [clean]}   # only the listed-callsign row is suppressed
