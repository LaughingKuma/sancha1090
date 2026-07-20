import datetime
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
START_S, END_S = 1784163600, 1784167200
START_DAY = datetime.date(2026, 7, 16)
HEAD_BEFORE = datetime.date(2026, 7, 15)   # start_day > head → post-head, provisional-eligible
HEAD_AFTER = datetime.date(2026, 7, 16)    # start_day <= head → historical pathless


@pytest.fixture(scope="module")
def livemap():
    spec = importlib.util.spec_from_file_location("livemap_app_prov", REPO_ROOT / "livemap" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def base(livemap, monkeypatch):
    original = livemap._fetch_provisional
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(livemap, "_path_cache", {})
    monkeypatch.setattr(
        livemap, "_fetch_path_auth",
        lambda _fid: ("abc123", "ANA1", False, START_S, END_S, START_DAY),
    )
    monkeypatch.setattr(livemap, "_fetch_path", lambda _fid: [])
    monkeypatch.setattr(livemap, "_path_head", {"expiry": float("inf"), "head": HEAD_BEFORE})
    monkeypatch.setattr(
        livemap, "_fetch_provisional",
        lambda *_a: (_ for _ in ()).throw(AssertionError("test must stub _fetch_provisional explicitly")),
    )
    return original


def _get(livemap, fid="42"):
    return TestClient(livemap.app).get(f"/path/{fid}")


def test_head_query_shape(livemap):
    assert f"max(day_key) FROM {livemap.CH_DB}.fct_flight_path" in livemap.PATH_HEAD_QUERY


def test_historical_pathless_returns_empty_and_caches(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_head", {"expiry": float("inf"), "head": HEAD_AFTER})
    r = _get(livemap)
    assert r.json() == {"flight_id": "42", "points": [], "provisional": False}
    assert livemap._path_cache[42][1] == []   # empty cached only AFTER the gate classified it historical


def test_post_head_settled_empty_not_cached(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_fetch_provisional", lambda *_a: [])
    r = _get(livemap)
    # empty fusion on a post-head flight is still provisional — and still never cached
    assert r.json() == {"flight_id": "42", "points": [], "provisional": True}
    assert livemap._path_cache == {}


def test_cold_head_fetch_failure_fails_closed(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_head", {"expiry": 0.0, "head": None})
    monkeypatch.setattr(livemap, "_fetch_path_head", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    r = _get(livemap)
    assert r.status_code == 200
    assert r.json() == {"flight_id": "42", "points": [], "provisional": False}
    assert r.headers["cache-control"] == "no-store"
    assert livemap._path_cache == {}


def test_expired_head_refresh_failure_keeps_last_good(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_head", {"expiry": 0.0, "head": HEAD_AFTER})
    monkeypatch.setattr(livemap, "_fetch_path_head", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert _get(livemap).json()["provisional"] is False   # stale head still classifies (historical here)
    assert livemap._path_cache[42][1] == []


def test_empty_cache_hit_does_not_short_circuit_the_gate(livemap, monkeypatch):
    # rev 6: classify-and-continue — a cached empty must reach the eligibility check, not return at the cache
    monkeypatch.setattr(livemap, "_path_cache", {42: (float("inf"), [])})
    monkeypatch.setattr(livemap, "_path_head", {"expiry": float("inf"), "head": HEAD_AFTER})
    monkeypatch.setattr(
        livemap, "_fetch_path",
        lambda _fid: (_ for _ in ()).throw(AssertionError("settled must not re-run on an unexpired hit")),
    )
    assert _get(livemap).json() == {"flight_id": "42", "points": [], "provisional": False}


def test_windowless_spine_row_fails_closed(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_fetch_path_auth", lambda _fid: ("abc123", "ANA1", False, None, None, None))
    r = _get(livemap)
    assert r.json() == {"flight_id": "42", "points": [], "provisional": False}
    assert livemap._path_cache == {}


def test_fuse_priority_per_second(livemap):
    rows = [
        (100, 2, 35.0, 139.0, 1000.0, 400.0, 90.0, 0, "adsblol"),
        (100, 1, 35.1, 139.1, 1100.0, 410.0, 91.0, 0, "adsb"),
        (101, 3, 35.2, 139.2, None, None, None, 0, "opensky"),
    ]
    assert livemap._fuse_points(rows) == [
        [139.1, 35.1, 100, 1100.0, "adsb"],
        [139.2, 35.2, 101, None, "opensky"],
    ]


def test_fuse_null_vs_numeric_same_second_is_nulls_last(livemap):
    # a naive tuple sort mixing None and float RAISES; CH's default is NULLS LAST — mirror it
    rows = [
        (100, 1, 35.0, 139.0, None, None, None, 0, "adsb"),
        (100, 1, 35.0, 139.0, 900.0, None, None, 0, "adsb"),
    ]
    assert livemap._fuse_points(rows)[0][3] == 900.0


def test_fuse_same_source_same_second_total_order(livemap):
    rows = [
        (100, 1, 35.5, 139.0, 1000.0, 400.0, 90.0, 0, "adsb"),
        (100, 1, 35.4, 139.0, 1000.0, 400.0, 90.0, 0, "adsb"),
    ]
    assert livemap._fuse_points(rows)[0][1] == 35.4   # lat breaks the tie — deterministic total order


def test_fuse_output_strictly_ascending(livemap):
    rows = [
        (300, 3, 35.0, 139.0, None, None, None, 0, "opensky"),
        (100, 1, 35.1, 139.1, 1.0, 2.0, 3.0, 0, "adsb"),
        (200, 2, 35.2, 139.2, 4.0, 5.0, 6.0, 1, "adsblol"),
    ]
    tss = [p[2] for p in livemap._fuse_points(rows)]
    assert tss == sorted(tss) == [100, 200, 300]


def test_contest_zero_competitors_keeps_every_fix(livemap):
    assert livemap._contest_keep(100, 42, 0, 200, []) is True


def test_contest_nearest_midpoint_wins(livemap):
    comp = [(7, 100, 200)]                                   # competitor midpoint 150, padded [-500, 800]
    assert livemap._contest_keep(200, 42, 0, 1000, comp) is False   # |200-150| < |200-500| → competitor's fix
    assert livemap._contest_keep(490, 42, 0, 1000, comp) is True    # |490-500| < |490-150| → ours


def test_contest_outside_competitor_padded_window_keeps_fix(livemap):
    comp = [(7, 100, 200)]                       # padded window ends at 200 + 600 = 800
    assert livemap._contest_keep(900, 42, 0, 2000, comp) is True    # never contested by a window not containing it


def test_contest_flight_id_tiebreak_is_total(livemap):
    comp_lo = [(7, 400, 1000)]                   # midpoint 700; ts 600 equidistant from 500 and 700
    comp_hi = [(99, 400, 1000)]
    assert livemap._contest_keep(600, 42, 0, 1000, comp_lo) is False  # 7 < 42 → competitor wins the tie
    assert livemap._contest_keep(600, 42, 0, 1000, comp_hi) is True   # 42 < 99 → we win the tie


PTS = [[139.7, 35.6, 1765500100, 38000.0, "adsb"]]


def test_post_head_serves_provisional(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_fetch_provisional", lambda *_a: PTS)
    r = _get(livemap)
    assert r.status_code == 200
    assert r.json() == {"flight_id": "42", "points": PTS, "provisional": True}
    assert r.headers["cache-control"] == "no-store"


def test_provisional_bypasses_path_cache(livemap, monkeypatch):
    calls = {"n": 0}
    def fetch(*_a):
        calls["n"] += 1
        return PTS
    monkeypatch.setattr(livemap, "_fetch_provisional", fetch)
    c = TestClient(livemap.app)
    c.get("/path/42")
    c.get("/path/42")
    assert calls["n"] == 2                      # recomputed every request — bronze grows all day
    assert livemap._path_cache == {}


def test_seeded_empty_cache_entry_does_not_suppress_fallback(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_path_cache", {42: (float("inf"), [])})
    monkeypatch.setattr(livemap, "_fetch_provisional", lambda *_a: PTS)
    assert _get(livemap).json()["provisional"] is True


def test_provisional_failure_fails_closed(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_fetch_provisional", lambda *_a: (_ for _ in ()).throw(RuntimeError("down")))
    r = _get(livemap)
    assert r.status_code == 200
    assert r.json() == {"flight_id": "42", "points": [], "provisional": False}
    assert r.headers["cache-control"] == "no-store"
    assert livemap._path_cache == {}


def test_ladd_mart_bit_blocks_provisional(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_fetch_path_auth",
                        lambda _fid: ("abc123", "ANA1", True, START_S, END_S, START_DAY))
    monkeypatch.setattr(livemap, "_fetch_provisional",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("suppressed must not fetch")))
    r = _get(livemap)
    assert r.json() == {"flight_id": "42", "points": [], "provisional": False}
    assert r.headers["cache-control"] == "no-store"   # the suppressed branch asserts no-store too


def test_ladd_live_hex_blocks_provisional(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", {"hex": frozenset({"abc123"}), "callsign": frozenset()})
    monkeypatch.setattr(livemap, "_fetch_provisional",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("suppressed must not fetch")))
    assert _get(livemap).json() == {"flight_id": "42", "points": [], "provisional": False}


def test_ladd_live_callsign_only_blocks_provisional(livemap, monkeypatch):
    # the 47k-row class: LADD rows with callsign but no hex — a hex-only belt silently drops two-thirds
    monkeypatch.setattr(livemap, "_ladd_suppress", {"hex": frozenset(), "callsign": frozenset({"ANA1"})})
    monkeypatch.setattr(livemap, "_fetch_provisional",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("suppressed must not fetch")))
    assert _get(livemap).json() == {"flight_id": "42", "points": [], "provisional": False}


def test_ladd_never_loaded_blocks_provisional(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", None)
    monkeypatch.setattr(livemap, "_fetch_provisional",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("must fail closed before auth")))
    assert _get(livemap).json() == {"flight_id": "42", "points": [], "provisional": False}


class _StageClient:
    # scripted per-query results; a value of Exception type raises — exercises per-stage fail-closed
    def __init__(self, script):
        self.script = script
    def query(self, sql, parameters=None):  # noqa: ARG002 (real clickhouse-connect client API)
        for frag, result in self.script:
            if frag in sql:
                if isinstance(result, type) and issubclass(result, Exception):
                    raise result("stage down")
                class R:
                    result_rows = result
                return R()
        raise AssertionError(f"unexpected query: {sql[:80]}")
    def close(self):
        pass


def test_competitor_lookup_failure_raises(livemap, monkeypatch, base):
    # a failed lookup must never read as "zero competitors" — it could serve a suppressed neighbor's fixes
    monkeypatch.setattr(livemap, "_ch_client",
                        lambda: _StageClient([("fct_flights_reconciled", RuntimeError)]))
    with pytest.raises(RuntimeError, match="stage down"):
        base(42, "abc123", START_S, END_S)


def test_partial_bronze_failure_raises(livemap, monkeypatch, base):
    monkeypatch.setattr(livemap, "_ch_client", lambda: _StageClient([
        ("fct_flights_reconciled", []),
        ("bronze.adsb_states", [(1765500100, 35.6, 139.7, 38000.0, 0, 450.0, 90.0)]),
        ("bronze.adsblol_flight_paths", RuntimeError),      # partial fusion must never serve
        ("bronze.opensky_states", []),
    ]))
    with pytest.raises(RuntimeError, match="stage down"):
        base(42, "abc123", START_S, END_S)


def test_fetch_provisional_clips_fuses_and_contests(livemap, monkeypatch, base):
    pad_only = START_S - 300          # inside the ±600 s pad, outside the raw window — must be KEPT
    outside = START_S - 601           # outside the padded window — dropped even if a scan returned it
    monkeypatch.setattr(livemap, "_ch_client", lambda: _StageClient([
        ("fct_flights_reconciled", []),
        ("bronze.adsb_states", [(START_S + 10, 35.6, 139.7, 38000.0, 0, 450.0, 90.0),
                                (outside, 35.0, 139.0, 1000.0, 0, 100.0, 10.0)]),
        ("bronze.adsblol_flight_paths", [(START_S + 10, 35.61, 139.71, 38010.0, 0, 451.0, 91.0),
                                         (pad_only, 35.5, 139.5, None, 0, None, None)]),
        ("bronze.opensky_states", [(START_S + 20, None, 139.8, 12000.0, 0, 300.0, 45.0)]),  # null geometry
    ]))
    out = base(42, "abc123", START_S, END_S)
    assert out == [
        [139.5, 35.5, pad_only, None, "adsblol"],           # pad-only fix kept
        [139.7, 35.6, START_S + 10, 38000.0, "adsb"],       # rooftop outranks adsblol on the shared second
    ]                                                        # outside-pad and null-geometry rows dropped


def test_competitor_query_ignores_is_ladd(livemap):
    # a suppressed neighbor must still win its own fixes away — nothing about it is emitted, so no oracle
    assert "is_ladd" not in livemap.PATH_COMPETITOR_QUERY


def test_provisional_query_shapes(livemap):
    adsb = livemap.PROVISIONAL_ADSB_QUERY
    assert "capture_date BETWEEN {day_lo:Date} AND {day_hi:Date}" in adsb   # physical leading-key prune
    assert "'ground'" in adsb                                               # rooftop sentinel, as the mart
    lol = livemap.PROVISIONAL_ADSBLOL_QUERY
    assert "trace_day BETWEEN {halo_lo:Date} AND {halo_hi:Date}" in lol     # D-1..D+1 primary-key prune
    osk = livemap.PROVISIONAL_OPENSKY_QUERY
    assert "snapshot_date BETWEEN {halo_lo:Date} AND {halo_hi:Date}" in osk # day-halo, never a fixed slack
    assert "snapshot_time >= toDateTime64({broad_lo:Int64}" in osk          # primary key LEADS with snapshot_time
    assert "coalesce(time_position, snapshot_time)" in osk                  # event-time clip does precision
    assert "3.28084" in osk and "1.94384" in osk                            # mart unit constants, verbatim
