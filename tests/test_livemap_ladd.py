import importlib.util
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


class _CHErr(RuntimeError):
    # Stand-in for a clickhouse-connect DatabaseError, which carries numeric code + symbolic name.
    def __init__(self, msg, code=None, name=None):
        super().__init__(msg)
        self.code = code
        self.name = name


@pytest.fixture(scope="module")
def livemap():
    spec = importlib.util.spec_from_file_location("livemap_app", REPO_ROOT / "livemap" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_is_ladd_suppressed_identity_and_flag(livemap):
    s = {"hex": frozenset({"abc123"}), "callsign": frozenset({"ANA1"})}
    f = livemap._is_ladd_suppressed
    assert f("ABC123", None, mv_is_ladd=False, suppress=s) is True     # case-folded hex match
    assert f("zzz999", "ana1 ", mv_is_ladd=False, suppress=s) is True  # trailing space: callsign trim+upper before match
    assert f("zzz999", "OTHER", mv_is_ladd=False, suppress=s) is False
    assert f("zzz999", None, mv_is_ladd=True, suppress=s) is True       # the MV belt alone suppresses


def test_is_ladd_suppressed_empty_set_drops_nothing(livemap):
    empty = livemap._EMPTY_SUPPRESS
    f = livemap._is_ladd_suppressed
    assert f("abc123", "ANA1", mv_is_ladd=False, suppress=empty) is False
    assert f(None, None, mv_is_ladd=False, suppress=empty) is False


def test_is_ladd_suppressed_none_state_only_mv_belt(livemap):
    f = livemap._is_ladd_suppressed
    # None = dim set never loaded: identity can't be checked, only the MV belt still suppresses.
    assert f("abc123", "ANA1", mv_is_ladd=False, suppress=None) is False
    assert f("abc123", "ANA1", mv_is_ladd=True, suppress=None) is True


def test_ladd_missing_table_detection(livemap):
    f = livemap._ladd_missing_table
    # structured signal: clickhouse-connect sets .code=60 / .name=UNKNOWN_TABLE on a missing relation
    assert f(_CHErr("Table dim.dim_ladd ...", code=60)) is True
    assert f(_CHErr("dim.dim_ladd gone", name="UNKNOWN_TABLE")) is True
    assert f(_CHErr("dim.dim_ladd gone", code="60")) is True                        # code may arrive as a string
    # string fallback when code/name are unavailable (suppressed error detail)
    assert f(RuntimeError("Code: 60. DB::Exception: Table dim.dim_ladd doesn't exist. (UNKNOWN_TABLE)")) is True
    # a DIFFERENT missing relation must NOT read as pre-deploy — scoped to dim_ladd so real outages surface
    assert f(_CHErr("Table dim.other doesn't exist", code=60, name="UNKNOWN_TABLE")) is False
    assert f(RuntimeError("connection refused")) is False


def test_is_unknown_table_error_structured_and_string(livemap):
    f = livemap._is_unknown_table_error
    assert f(_CHErr("x", code=60)) is True
    assert f(_CHErr("x", name="unknown_table")) is True                             # name check is case-folded
    assert f(RuntimeError("Code: 60. DB::Exception ... (UNKNOWN_TABLE)")) is True
    assert f(_CHErr("x", code=47)) is False                                         # UNKNOWN_IDENTIFIER, not table
    assert f(RuntimeError("timed out")) is False


def test_refresh_success_replaces_cache(livemap, monkeypatch, tmp_path):
    fresh = {"hex": frozenset({"deadbe"}), "callsign": frozenset({"XYZ"})}
    monkeypatch.setattr(livemap, "_fetch_ladd_suppress", lambda: fresh)
    cache = tmp_path / "c.json"
    monkeypatch.setattr(livemap, "LADD_CACHE_PATH", str(cache))
    assert livemap._refresh_ladd_suppress(livemap._EMPTY_SUPPRESS) is fresh
    # a successful refresh persists last-good so a cold-start restart reseeds it instead of failing open
    assert livemap._read_ladd_cache(str(cache)) == fresh


def test_ladd_cache_round_trip(livemap, tmp_path):
    p = str(tmp_path / "c.json")
    s = {"hex": frozenset({"abc123", "deadbe"}), "callsign": frozenset({"ANA1"})}
    livemap._write_ladd_cache(s, p)
    assert livemap._read_ladd_cache(p) == s


def test_ladd_cache_missing_is_none(livemap, tmp_path):
    assert livemap._read_ladd_cache(str(tmp_path / "nope.json")) is None


def test_ladd_cache_corrupt_is_none(livemap, tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert livemap._read_ladd_cache(str(p)) is None
    p.write_text('{"hex": 5, "callsign": []}')      # right keys, wrong shape (int isn't iterable) -> None
    assert livemap._read_ladd_cache(str(p)) is None


def test_boot_seeds_from_cache_counts_as_loaded(tmp_path, monkeypatch):
    cache = tmp_path / "ladd_cache.json"
    cache.write_text(json.dumps({"hex": ["abc123"], "callsign": ["ANA1"]}))
    monkeypatch.setenv("LIVEMAP_LADD_CACHE_PATH", str(cache))
    spec = importlib.util.spec_from_file_location("livemap_boot", REPO_ROOT / "livemap" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # boot-load is a real loaded state (not None): dim filtering is active immediately
    assert mod._ladd_suppress == {"hex": frozenset({"abc123"}), "callsign": frozenset({"ANA1"})}
    assert mod._is_ladd_suppressed("ABC123", None, mv_is_ladd=False, suppress=mod._ladd_suppress) is True
    # ... and /track no longer fails closed for everything: a clean hex reaches RW and serves
    monkeypatch.setattr(mod, "_fetch_track", lambda _icao: [[1.0, 2.0, 3.0, 4]])
    r = TestClient(mod.app).get("/track/CLEAN9")
    assert r.status_code == 200
    assert r.json() == {"hex": "CLEAN9", "points": [[1.0, 2.0, 3.0, 4]]}


def test_refresh_error_keeps_current(livemap, monkeypatch):
    good = {"hex": frozenset({"abc123"}), "callsign": frozenset()}

    def boom():
        raise RuntimeError("ch down")

    monkeypatch.setattr(livemap, "_fetch_ladd_suppress", boom)
    # a real refresh error must retain the prior state, never fall open to empty
    assert livemap._refresh_ladd_suppress(good) is good
    # None must stay None so /track keeps failing closed until a genuine load
    assert livemap._refresh_ladd_suppress(None) is None


def test_refresh_missing_table_is_empty_load(livemap, monkeypatch):
    def boom():
        raise RuntimeError("Table dim.dim_ladd doesn't exist")

    monkeypatch.setattr(livemap, "_fetch_ladd_suppress", boom)
    # pre-deploy: a missing table is a *successful* empty load, transitioning None -> loaded-empty
    assert livemap._refresh_ladd_suppress(None) is livemap._EMPTY_SUPPRESS


def test_should_refresh_ladd_cadence(livemap):
    f = livemap._should_refresh_ladd
    # None (never loaded) retries on every tick until the first success closes the fail-open window
    assert f(None, 1) is True
    assert f(None, 7) is True
    # once loaded (even empty) it only refreshes on the ~15-min tick boundary
    assert f(livemap._EMPTY_SUPPRESS, 0) is True
    assert f(livemap._EMPTY_SUPPRESS, 1) is False


def test_track_belt_suppressed_ttl(livemap):
    now = 1000.0
    fresh = {"beef00": now - 10}
    stale = {"beef00": now - livemap.HISTORY_BUFFER_S - 1}
    assert livemap._track_belt_suppressed("BEEF00", now, fresh) is True    # seen within the TTL
    assert livemap._track_belt_suppressed("BEEF00", now, stale) is False   # aged out of the TTL
    assert livemap._track_belt_suppressed("other", now, fresh) is False


def test_fetch_drops_suppressed_rows_and_records_mv_belt(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress",
                        {"hex": frozenset({"deadbe"}), "callsign": frozenset({"SECRET1"})})
    monkeypatch.setattr(livemap, "_mv_ladd_hexes", {})
    rows = [
        {"capture_ts": None, "hex": "deadbe", "flight": "AAA1", "is_ladd": False, "nav_modes": None},     # hex
        {"capture_ts": None, "hex": "abc123", "flight": "SECRET1 ", "is_ladd": False, "nav_modes": None},  # callsign
        {"capture_ts": None, "hex": "beef00", "flight": "ANA55", "is_ladd": True, "nav_modes": None},      # MV flag
        {"capture_ts": None, "hex": "cafe11", "flight": "JAL9", "is_ladd": False, "nav_modes": None},      # clean
    ]
    monkeypatch.setattr(livemap, "_rw_rows", lambda *_a, **_k: rows)
    out = livemap._fetch()["aircraft"]
    assert [a["hex"] for a in out] == ["cafe11"]
    assert "is_ladd" not in out[0]                    # the flag must never ride the client payload
    assert "beef00" in livemap._mv_ladd_hexes         # MV-belt drop recorded so /track can fail closed for it


def test_track_suppressed_hex_returns_empty_without_hitting_rw(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", {"hex": frozenset({"abc123"}), "callsign": frozenset()})
    monkeypatch.setattr(livemap, "_mv_ladd_hexes", {})

    def boom(_icao):
        raise AssertionError("a suppressed hex must not reach RW")

    monkeypatch.setattr(livemap, "_fetch_track", boom)
    r = TestClient(livemap.app).get("/track/ABC123")
    assert r.status_code == 200
    assert r.json() == {"hex": "ABC123", "points": []}


def test_track_belt_suppressed_hex_returns_empty_without_hitting_rw(livemap, monkeypatch):
    # loaded-but-empty dim set, yet the hex is on the live MV belt -> /track still fails closed
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(livemap, "_mv_ladd_hexes", {"beef00": time.time()})

    def boom(_icao):
        raise AssertionError("a belt-suppressed hex must not reach RW")

    monkeypatch.setattr(livemap, "_fetch_track", boom)
    r = TestClient(livemap.app).get("/track/BEEF00")
    assert r.status_code == 200
    assert r.json() == {"hex": "BEEF00", "points": []}


def test_track_none_state_fails_closed_for_all(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", None)

    def boom(_icao):
        raise AssertionError("None-state /track must not reach RW")

    monkeypatch.setattr(livemap, "_fetch_track", boom)
    r = TestClient(livemap.app).get("/track/ANY999")
    assert r.status_code == 200
    assert r.json() == {"hex": "ANY999", "points": []}


def test_flights_query_excludes_ladd(livemap):
    assert "is_ladd = 0" in livemap.FLIGHTS_QUERY   # window-aware mart flag filters history


def test_aircraft_query_selects_is_ladd(livemap):
    m = re.search(r"SELECT(?P<sel>.*?)FROM\s+mv_current_aircraft", livemap.QUERY, flags=re.I | re.S)
    assert m and re.search(r"\bis_ladd\b", m.group("sel"), flags=re.I)
