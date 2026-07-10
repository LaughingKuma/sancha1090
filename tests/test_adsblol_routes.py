from __future__ import annotations

import gzip as _gzip
import json
from datetime import date, timedelta
from pathlib import Path

import include.adsblol_routes as routes

FIXTURE = Path(__file__).parent / "fixtures" / "trace_full_a61c53_2026-06-25.json"
DAY = date(2026, 6, 25)


def _doc():
    return json.loads(FIXTURE.read_text())


def _synthetic(points, icao="abc123", base=1782345600):
    return {"icao": icao, "timestamp": base, "trace": points}


def test_real_trace_splits_into_rotation_legs():
    segs = routes.trace_segments(_doc(), DAY)
    # ->ANC, ANC->LAX, LAX->(cut at midnight): the 7h Anchorage ground stop and the
    # LAX turnaround both exceed GAP_SPLIT_S.
    assert len(segs) >= 3
    assert all(s["icao24"] == "a61c53" for s in segs)
    assert all(s["trace_day"] == "2026-06-25" and s["source"] == "adsblol" for s in segs)


def test_real_trace_anchorage_leg_endpoint():
    segs = routes.trace_segments(_doc(), DAY)
    anc = min(segs, key=lambda s: abs(s["last_lat"] - 61.17) + abs(s["last_lon"] + 150.33))
    assert abs(anc["last_lat"] - 61.17) < 0.5
    assert abs(anc["last_lon"] + 150.33) < 0.5
    assert anc["last_alt_ft"] < 3500
    assert anc["callsign"] == "GTI518"


def test_real_trace_final_leg_cut_at_midnight_stays_at_cruise():
    segs = routes.trace_segments(_doc(), DAY)
    last = max(segs, key=lambda s: s["seg_end"])
    assert last["last_alt_ft"] > 10000  # unsnappable endpoint -> stays NULL downstream


def test_gap_splits_segment():
    p1 = [0.0, 10.0, 100.0, 30000, 450, 90, 0, 0, None, "adsb_icao", 30000, 0, 0, 0]
    p2 = [100.0, 10.5, 100.5, 30000, 450, 90, 0, 0, None, "adsb_icao", 30000, 0, 0, 0]
    p3 = [100.0 + routes.GAP_SPLIT_S + 1, 20.0, 110.0, 30000, 450, 90, 0, 0, None,
          "adsb_icao", 30000, 0, 0, 0]
    p4 = [200.0 + routes.GAP_SPLIT_S, 20.5, 110.5, 30000, 450, 90, 0, 0, None,
          "adsb_icao", 30000, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([p1, p2, p3, p4]), DAY)
    assert len(segs) == 2
    assert segs[0]["num_fixes"] == 2 and segs[1]["num_fixes"] == 2


def test_ground_contact_after_air_splits_segment():
    air1 = [0.0, 10.0, 100.0, 5000, 200, 90, 0, -500, None, "adsb_icao", 5000, 0, 0, 0]
    air2 = [60.0, 10.1, 100.1, 2000, 150, 90, 0, -500, None, "adsb_icao", 2000, 0, 0, 0]
    gnd = [120.0, 10.2, 100.2, "ground", 5, 90, 0, 0, None, "adsb_icao", 0, 0, 0, 0]
    air3 = [600.0, 10.2, 100.3, 1000, 150, 90, 0, 500, None, "adsb_icao", 1000, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([air1, air2, gnd, air3]), DAY)
    assert len(segs) == 2
    # The landing's ground fix opens the NEXT segment (same split point as
    # fct_flight_legs), so segment 2 starts on the ground at the airport.
    assert segs[0]["last_alt_ft"] == 2000
    assert segs[1]["first_on_ground"] is True
    assert segs[1]["first_alt_ft"] == 0.0


def test_repeated_fix_flag_skipped_and_ground_only_dropped():
    stale = [0.0, 10.0, 100.0, 30000, 450, 90, 1, 0, None, "adsb_icao", 30000, 0, 0, 0]
    g1 = [10.0, 10.0, 100.0, "ground", 5, 90, 0, 0, None, "adsb_icao", 0, 0, 0, 0]
    g2 = [20.0, 10.0, 100.0, "ground", 5, 90, 0, 0, None, "adsb_icao", 0, 0, 0, 0]
    assert routes.trace_segments(_synthetic([stale, g1, g2]), DAY) == []


def test_dominant_callsign_ties_break_lexically():
    def pt(t, flight):
        return [t, 10.0, 100.0, 30000, 450, 90, 0, 0,
                {"type": "adsb_icao", "flight": flight}, "adsb_icao", 30000, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([pt(0, "BBB2  "), pt(10, "AAA1  "),
                                             pt(20, "AAA1  "), pt(30, "BBB2  ")]), DAY)
    assert len(segs) == 1
    assert segs[0]["callsign"] == "AAA1"


def test_empty_and_synthetic_hex_rejected():
    assert routes.trace_segments({"icao": "~a1b2c3", "timestamp": 0, "trace": [[0, 1, 2, 3]]}, DAY) == []
    assert routes.trace_segments({"icao": "abc123", "timestamp": 0, "trace": []}, DAY) == []


def test_segments_frame_schema_and_ingested_at():
    segs = routes.trace_segments(_doc(), DAY)
    df = routes.segments_frame(segs)
    assert df.height == len(segs)
    assert set(routes.RAW_SEGMENTS_SCHEMA) | {"ingested_at"} == set(df.columns)
    empty = routes.segments_frame([])
    assert empty.height == 0 and "ingested_at" in empty.columns


def test_trace_paths_bins_every_segment_fix():
    doc = _doc()
    segs = routes.trace_segments(doc, DAY)
    pts = routes.trace_paths(doc, DAY, segs)
    assert pts
    spans = {s["seg_start"]: s for s in segs}
    assert all(p["seg_start"] in spans for p in pts)
    assert all(spans[p["seg_start"]]["seg_start"] <= p["ts"] <= spans[p["seg_start"]]["seg_end"] for p in pts)
    # Same filters as the sessionizer -> per-segment point count equals num_fixes.
    for s in segs:
        assert sum(1 for p in pts if p["seg_start"] == s["seg_start"]) == s["num_fixes"]


def test_trace_paths_drops_points_outside_segments():
    g1 = [10.0, 10.0, 100.0, "ground", 5, 90, 0, 0, None, "adsb_icao", 0, 0, 0, 0]
    g2 = [20.0, 10.0, 100.0, "ground", 5, 90, 0, 0, None, "adsb_icao", 0, 0, 0, 0]
    doc = _synthetic([g1, g2])
    assert routes.trace_paths(doc, DAY, routes.trace_segments(doc, DAY)) == []


def test_paths_frame_schema():
    doc = _doc()
    segs = routes.trace_segments(doc, DAY)
    df = routes.paths_frame(routes.trace_paths(doc, DAY, segs))
    assert df.height > 0
    assert set(routes.RAW_PATHS_SCHEMA) | {"ingested_at"} == set(df.columns)


class _FakeResp:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise routes.requests.HTTPError(str(self.status_code))


class _FakeSession:
    def __init__(self, handler):
        self._handler = handler
        self.seen = {}

    def get(self, url, headers=None, timeout=None):  # noqa: ARG002 (headers/timeout keyword-bound in fetch_trace)
        self.seen["url"] = url
        return self._handler(url)


def test_fetch_trace_decompresses_gzip_and_builds_sharded_url():
    body = _gzip.compress(b'{"icao": "a61c53", "timestamp": 1, "trace": []}')
    session = _FakeSession(lambda _url: _FakeResp(200, body))
    doc = routes.fetch_trace(DAY, "a61c53", session=session)
    assert doc["icao"] == "a61c53"
    assert session.seen["url"] == "https://globe.adsb.lol/globe_history/2026/06/25/traces/53/trace_full_a61c53.json"


def test_fetch_trace_404_means_no_trace():
    session = _FakeSession(lambda _url: _FakeResp(404, b""))
    assert routes.fetch_trace(DAY, "deadbe", session=session) is None


def test_fetch_trace_raises_after_retries(monkeypatch):
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _boom(_url):
        calls["n"] += 1
        raise routes.requests.ConnectionError("boom")

    session = _FakeSession(_boom)
    import pytest
    with pytest.raises(RuntimeError):
        routes.fetch_trace(DAY, "a61c53", session=session)
    assert calls["n"] == 3


def test_run_daily_fetches_day_and_prior_lands_and_records(monkeypatch):
    import sqlalchemy as sa

    import include.adsblol_route_ledger as ledger

    eng = sa.create_engine("sqlite://")
    ledger.ensure_table(eng)
    monkeypatch.setattr(routes, "route_targets", lambda _day, **_kw: ["a61c53"])
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)

    fetched = []

    def fake_fetch(day, hexid, **_kw):
        fetched.append((hexid, day.isoformat()))
        return _doc() if day == DAY else None  # D-1 missing

    written = {}
    monkeypatch.setattr(routes, "write_parquet",
                        lambda df, key: written.update({key: df.height}) or f"s3://b/{key}")
    recorded = []
    monkeypatch.setattr(routes.manifest, "record_load",
                        lambda uri, _smin, _smax, rows, engine=None:  # noqa: ARG005 (engine kw-bound)
                        recorded.append((uri, rows)))

    out = routes.run_daily(DAY, engine=eng, fetch=fake_fetch)
    assert set(fetched) == {("a61c53", "2026-06-25"), ("a61c53", "2026-06-24")}
    assert out["rows"] > 0 and out["path_rows"] > 0
    import re
    seg_keys = [k for k in written if k.startswith("bronze/adsblol_flight_segments/")]
    path_keys = [k for k in written if k.startswith("bronze/adsblol_flight_paths/")]
    assert len(seg_keys) == 1
    assert len(path_keys) == 1
    # Per-run stamp (v6.10): a same-day rerun must land a NEW object — a rewrite of a drained
    # key never re-drains (record_load preserves ch_loaded_at).
    m = re.fullmatch(r"bronze/adsblol_flight_segments/dt=2026-06-25/part-(\d{8}T\d{12})\.parquet", seg_keys[0])
    assert m, seg_keys[0]
    assert path_keys[0] == f"bronze/adsblol_flight_paths/dt=2026-06-25/part-{m.group(1)}.parquet"
    assert {r for _, r in recorded} == {out["rows"], out["path_rows"]}
    # Both attempts recorded: the landed day and the missing D-1.
    assert ledger.filter_unattempted(
        [("a61c53", "2026-06-25"), ("a61c53", "2026-06-24")], eng) == []


def test_run_daily_reports_progress(monkeypatch):
    import sqlalchemy as sa

    import include.adsblol_route_ledger as ledger

    eng = sa.create_engine("sqlite://")
    ledger.ensure_table(eng)
    monkeypatch.setattr(routes, "route_targets", lambda _day, **_kw: ["a61c53", "abc123"])
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)

    seen = []
    # 2 hexes x (D, D-1) = 4 pairs; progress must fire once per pair, ending on (total, total).
    routes.run_daily(DAY, engine=eng, fetch=lambda *_a, **_k: None,
                     progress=lambda done, total: seen.append((done, total)))
    assert [d for d, _ in seen] == [1, 2, 3, 4]
    assert all(t == 4 for _, t in seen)
    assert seen[-1] == (4, 4)


def test_run_daily_explicit_targets_skips_route_query(monkeypatch):
    import sqlalchemy as sa

    import include.adsblol_route_ledger as ledger

    eng = sa.create_engine("sqlite://")
    ledger.ensure_table(eng)
    # An explicit target list must bypass route_targets entirely (backfill re-segment path).
    monkeypatch.setattr(routes, "route_targets",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("route_targets must not be called")))
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)

    fetched = []

    def fake_fetch(day, hexid, **_kw):
        fetched.append((hexid, day.isoformat()))
        if hexid == "ffff01":
            raise RuntimeError("trace fetch kept failing")
        if hexid == "deadbe":
            return None  # no trace for this hex that day -> missing
        return _doc()

    written = {}
    monkeypatch.setattr(routes, "write_parquet",
                        lambda df, key: written.update({key: df.height}) or f"s3://b/{key}")
    monkeypatch.setattr(routes.manifest, "record_load", lambda *_a, **_kw: None)

    out = routes.run_daily(DAY, targets=["a61c53", "deadbe", "ffff01"], engine=eng, fetch=fake_fetch)
    # Explicit targets fetch the given day ONLY -- no D-1 companion (each trace-day re-segments
    # independently and every affected D-1 already appears in the target list in its own right).
    assert set(fetched) == {("a61c53", "2026-06-25"), ("deadbe", "2026-06-25"), ("ffff01", "2026-06-25")}
    assert out["fetched"] == 3
    assert out["landed"] == 1
    assert out["missing"] == 1
    assert out["errors"] == 1
    assert out["rows"] > 0
    seg_keys = [k for k in written if k.startswith("bronze/adsblol_flight_segments/")]
    assert len(seg_keys) == 1


def test_run_daily_exposes_landed_hexes_only(monkeypatch):
    import sqlalchemy as sa

    import include.adsblol_route_ledger as ledger

    eng = sa.create_engine("sqlite://")
    ledger.ensure_table(eng)
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    monkeypatch.setattr(routes, "write_parquet", lambda _df, key: f"s3://b/{key}")
    monkeypatch.setattr(routes.manifest, "record_load", lambda *_a, **_kw: None)

    def fake_fetch(_day, hexid, **_kw):
        if hexid == "ffff01":
            raise RuntimeError("trace fetch kept failing")
        if hexid == "deadbe":
            return None  # missing
        return _doc()

    out = routes.run_daily(DAY, targets=["a61c53", "deadbe", "ffff01"], engine=eng, fetch=fake_fetch)
    # Only the landed hex is exposed for supersede-deletion; missing/error hexes are absent.
    assert out["landed_hexes"] == ["a61c53"]
    assert out["landed"] == 1 and out["missing"] == 1 and out["errors"] == 1


def test_run_daily_concurrent_fetches_each_pair_once(monkeypatch):
    import threading

    import sqlalchemy as sa

    import include.adsblol_route_ledger as ledger

    eng = sa.create_engine("sqlite://")
    ledger.ensure_table(eng)
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    monkeypatch.setattr(routes, "write_parquet", lambda _df, key: f"s3://b/{key}")
    monkeypatch.setattr(routes.manifest, "record_load", lambda *_a, **_kw: None)

    lock = threading.Lock()
    fetched = []
    thread_names = set()

    def fake_fetch(day, hexid, **_kw):
        with lock:
            fetched.append((hexid, day.isoformat()))
            thread_names.add(threading.current_thread().name)
        return _doc()

    targets = ["a61c53", "abc123", "def456", "111222"]
    out = routes.run_daily(DAY, targets=targets, engine=eng, fetch=fake_fetch, workers=3)
    # Explicit targets -> DAY only; every pair fetched exactly once across the pool.
    assert sorted(fetched) == sorted((h, "2026-06-25") for h in targets)
    assert out["fetched"] == 4
    assert out["landed"] == 4
    assert out["rows"] > 0
    # Fetches ran on pool workers, never the main thread -> concurrency actually engaged.
    assert "MainThread" not in thread_names


def test_run_daily_concurrent_counts_an_erroring_pair(monkeypatch):
    import sqlalchemy as sa

    import include.adsblol_route_ledger as ledger

    eng = sa.create_engine("sqlite://")
    ledger.ensure_table(eng)
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    monkeypatch.setattr(routes, "write_parquet", lambda _df, key: f"s3://b/{key}")
    monkeypatch.setattr(routes.manifest, "record_load", lambda *_a, **_kw: None)

    def fake_fetch(_day, hexid, **_kw):
        if hexid == "ffff01":
            raise RuntimeError("trace fetch kept failing")
        if hexid == "deadbe":
            return None
        return _doc()

    out = routes.run_daily(DAY, targets=["a61c53", "deadbe", "ffff01"], engine=eng,
                           fetch=fake_fetch, workers=3)
    # A persistently-failing pair among concurrent fetches still tallies as one error, and the
    # good pair still lands (outcome semantics identical to the serial path).
    assert out["fetched"] == 3
    assert out["landed"] == 1
    assert out["missing"] == 1
    assert out["errors"] == 1
    assert out["rows"] > 0
    assert ledger.filter_unattempted(
        [("a61c53", "2026-06-25"), ("deadbe", "2026-06-25"), ("ffff01", "2026-06-25")], eng) == []


def test_run_daily_skips_ledgered_pairs(monkeypatch):
    import sqlalchemy as sa

    import include.adsblol_route_ledger as ledger

    eng = sa.create_engine("sqlite://")
    ledger.ensure_table(eng)
    ledger.record_attempts([("a61c53", "2026-06-25", "landed"),
                            ("a61c53", "2026-06-24", "landed")], eng)
    monkeypatch.setattr(routes, "route_targets", lambda _day, **_kw: ["a61c53"])
    out = routes.run_daily(DAY, engine=eng,
                           fetch=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("fetched")))
    assert out["fetched"] == 0 and out["uri"] is None


def test_run_daily_isolates_a_persistently_failing_pair(monkeypatch):
    import sqlalchemy as sa

    import include.adsblol_route_ledger as ledger

    eng = sa.create_engine("sqlite://")
    ledger.ensure_table(eng)
    monkeypatch.setattr(routes, "route_targets", lambda _day, **_kw: ["a61c53"])
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)

    def fake_fetch(day, _hexid, **_kw):
        if day == DAY - timedelta(days=1):  # D-1 is the persistently-failing pair
            raise RuntimeError("trace fetch kept failing")
        return _doc()

    written = []
    monkeypatch.setattr(routes, "write_parquet",
                        lambda _df, key: written.append(key) or f"s3://b/{key}")
    monkeypatch.setattr(routes.manifest, "record_load", lambda *_a, **_kw: None)

    out = routes.run_daily(DAY, engine=eng, fetch=fake_fetch)
    assert out["rows"] > 0  # the good pair still landed despite the other erroring
    assert written  # write_parquet was reached, i.e. the run wasn't discarded
    # Both pairs recorded in the ledger: 'error' behaves like 'missing' for filtering,
    # so both are inside their cooldown right after the run.
    assert ledger.filter_unattempted(
        [("a61c53", "2026-06-25"), ("a61c53", "2026-06-24")], eng) == []


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.seen = {}

    def query(self, sql, parameters=None):
        self.seen["sql"] = sql
        self.seen["parameters"] = parameters
        return _FakeResult(self._rows)


def test_route_targets_overlaps_on_either_endpoint():
    # SQL lower()s icao24; the fake returns rows already lowered as the driver would.
    fake = _FakeClient([("a61c53",), ("abc123",), ("a61c53",)])
    out = routes.route_targets(DAY, client=fake)
    sql = fake.seen["sql"]
    # Overlap predicate targets both departure-day and arrival-day flights.
    assert "toDate(start_time) = %(day)s" in sql
    assert "toDate(end_time) = %(day)s" in sql
    assert "origin_icao IS NULL OR dest_icao IS NULL" in sql
    assert "icao24 IS NOT NULL" in sql
    assert fake.seen["parameters"] == {"day": DAY.isoformat()}
    assert out == ["a61c53", "abc123"]  # deduped + sorted


def test_routes_sql_reads_reconciled():
    import include.flight_routes as fr

    sql = fr._routes_sql()
    assert "fct_flights_reconciled" in sql          # single consensus source (SP2)
    assert "int_flight_chains_adsblol" not in sql   # the fact_flights + adsblol union is gone
    assert "UNION" not in sql.upper()


def test_low_fix_gap_splits_missed_landing():
    # Out-and-back with NO "ground" fix at the far airport: approach fix low, 46-min silent
    # turnaround, then departure climb — must split into two one-way segments.
    out1 = [0.0, 35.55, 139.78, 5000, 250, 180, 0, -800, None, "adsb_icao", 5000, 0, 0, 0]
    out2 = [1200.0, 34.0, 136.0, 900, 140, 180, 0, -600, None, "adsb_icao", 900, 0, 0, 0]
    back1 = [1200.0 + 46 * 60, 34.0, 136.0, 800, 150, 0, 0, 900, None, "adsb_icao", 800, 0, 0, 0]
    back2 = [1200.0 + 46 * 60 + 1200.0, 35.55, 139.78, 4000, 250, 0, 0, -500, None, "adsb_icao", 4000, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([out1, out2, back1, back2]), DAY)
    assert len(segs) == 2
    assert segs[0]["last_alt_ft"] == 900
    assert segs[1]["first_alt_ft"] == 800


def test_low_fix_gap_both_sides_cruise_does_not_split():
    # Same 46-min gap but both boundary fixes at cruise = coverage hole, NOT a landing.
    a = [0.0, 35.55, 139.78, 33000, 450, 180, 0, 0, None, "adsb_icao", 33000, 0, 0, 0]
    b = [46 * 60.0, 34.0, 136.0, 34000, 450, 180, 0, 0, None, "adsb_icao", 34000, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a, b]), DAY)
    assert len(segs) == 1


def test_low_fix_short_gap_does_not_split():
    # Low fix but only a 20-min gap (holding / approach re-sequence) stays one segment.
    a = [0.0, 35.55, 139.78, 900, 150, 180, 0, -500, None, "adsb_icao", 900, 0, 0, 0]
    b = [20 * 60.0, 35.60, 139.80, 1500, 180, 0, 0, 500, None, "adsb_icao", 1500, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a, b]), DAY)
    assert len(segs) == 1


def test_low_fix_gap_none_altitude_fails_open():
    # A None altitude on both sides of the gap must NOT count as low (mirrors SQL coalesce 99999).
    a = [0.0, 35.55, 139.78, "", 250, 180, 0, 0, None, "adsb_icao", None, 0, 0, 0]
    b = [46 * 60.0, 34.0, 136.0, "", 250, 180, 0, 0, None, "adsb_icao", None, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a, b]), DAY)
    assert len(segs) == 1


def test_trace_paths_lockstep_on_low_fix_split():
    # Both loops must agree on the new boundary: every fix bins to a surviving segment.
    out1 = [0.0, 35.55, 139.78, 5000, 250, 180, 0, -800, None, "adsb_icao", 5000, 0, 0, 0]
    out2 = [1200.0, 34.0, 136.0, 900, 140, 180, 0, -600, None, "adsb_icao", 900, 0, 0, 0]
    back1 = [1200.0 + 46 * 60, 34.0, 136.0, 800, 150, 0, 0, 900, None, "adsb_icao", 800, 0, 0, 0]
    back2 = [1200.0 + 46 * 60 + 1200.0, 35.55, 139.78, 4000, 250, 0, 0, -500, None, "adsb_icao", 4000, 0, 0, 0]
    doc = _synthetic([out1, out2, back1, back2])
    segs = routes.trace_segments(doc, DAY)
    pts = routes.trace_paths(doc, DAY, segs)
    assert {p["seg_start"] for p in pts} == {s["seg_start"] for s in segs}
    for s in segs:
        assert sum(1 for p in pts if p["seg_start"] == s["seg_start"]) == s["num_fixes"]


def test_slow_gap_splits_hidden_landing():
    # No ground/low fix bookends the turnaround: a 35-min silence (>= SLOW_GAP_S, < LOW_FIX_GAP_S)
    # then a climb fix ~5 km away -> implied ~8.5 km/h means the aircraft landed inside the gap.
    out1 = [0.0, 34.00, 136.00, 6500, 200, 180, 0, -700, None, "adsb_icao", 6500, 0, 0, 0]
    out2 = [60.0, 34.00, 136.00, 6000, 180, 180, 0, -600, None, "adsb_icao", 6000, 0, 0, 0]
    back1 = [60.0 + 35 * 60, 34.045, 136.00, 7000, 160, 0, 0, 700, None, "adsb_icao", 7000, 0, 0, 0]
    back2 = [60.0 + 35 * 60 + 60, 34.10, 136.00, 8000, 200, 0, 0, 600, None, "adsb_icao", 8000, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([out1, out2, back1, back2]), DAY)
    assert len(segs) == 2
    assert segs[0]["last_alt_ft"] == 6000
    assert segs[1]["first_alt_ft"] == 7000


def test_slow_gap_at_cruise_does_not_split():
    # Same 35-min slow gap but both boundary fixes at cruise = coverage void, NOT a landing:
    # the cruise ceiling (SLOW_GAP_CEIL_FT) must veto even though the implied speed is slow.
    a = [0.0, 34.0, 136.0, 33000, 450, 180, 0, 0, None, "adsb_icao", 33000, 0, 0, 0]
    b = [35 * 60.0, 34.18, 136.0, 34000, 450, 180, 0, 0, None, "adsb_icao", 34000, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a, b]), DAY)
    assert len(segs) == 1


def test_slow_gap_fast_crossing_does_not_split():
    # 35-min gap, low fixes, but ~200 km apart -> implied ~343 km/h: a real flight crossing a
    # coverage hole, not a stop. The speed gate must veto.
    a = [0.0, 34.0, 136.0, 5000, 300, 180, 0, 0, None, "adsb_icao", 5000, 0, 0, 0]
    b = [35 * 60.0, 35.8, 136.0, 6000, 300, 180, 0, 0, None, "adsb_icao", 6000, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a, b]), DAY)
    assert len(segs) == 1


def test_slow_gap_below_floor_does_not_split():
    # A 25-min gap (< SLOW_GAP_S), low and near = holding / go-around, not a landing: the floor
    # preserves the v6.18 trade even when the geometry is otherwise slow-gap-shaped.
    a = [0.0, 34.0, 136.0, 1500, 150, 180, 0, -500, None, "adsb_icao", 1500, 0, 0, 0]
    b = [25 * 60.0, 34.045, 136.0, 1600, 160, 0, 0, 500, None, "adsb_icao", 1600, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a, b]), DAY)
    assert len(segs) == 1


def test_slow_gap_none_altitude_fails_open():
    # Both altitudes None across a slow, near gap: coalesce to 99999 -> above the cruise ceiling,
    # so the arm fails open and does NOT split (mirrors the SQL coalesce).
    a = [0.0, 34.0, 136.0, "", 150, 180, 0, 0, None, "adsb_icao", None, 0, 0, 0]
    b = [35 * 60.0, 34.045, 136.0, "", 160, 0, 0, 0, None, "adsb_icao", None, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a, b]), DAY)
    assert len(segs) == 1


def test_trace_paths_lockstep_on_slow_gap_split():
    # Both loops must agree on the slow-gap boundary: every fix bins to a surviving segment.
    out1 = [0.0, 34.00, 136.00, 6500, 200, 180, 0, -700, None, "adsb_icao", 6500, 0, 0, 0]
    out2 = [60.0, 34.00, 136.00, 6000, 180, 180, 0, -600, None, "adsb_icao", 6000, 0, 0, 0]
    back1 = [60.0 + 35 * 60, 34.045, 136.00, 7000, 160, 0, 0, 700, None, "adsb_icao", 7000, 0, 0, 0]
    back2 = [60.0 + 35 * 60 + 60, 34.10, 136.00, 8000, 200, 0, 0, 600, None, "adsb_icao", 8000, 0, 0, 0]
    doc = _synthetic([out1, out2, back1, back2])
    segs = routes.trace_segments(doc, DAY)
    pts = routes.trace_paths(doc, DAY, segs)
    assert {p["seg_start"] for p in pts} == {s["seg_start"] for s in segs}
    for s in segs:
        assert sum(1 for p in pts if p["seg_start"] == s["seg_start"]) == s["num_fixes"]


def test_slow_gap_persisted_grid_fires_on_truncated_1800():
    # Raw wall-clock gap 1799.49 s (< SLOW_GAP_S) but the persisted whole-second ts differ by exactly
    # 1800: the arm evaluates on that integer grid (matching AFFECTED_SQL), so it splits.
    a1 = [90.0, 34.00, 136.00, 6000, 200, 180, 0, -600, None, "adsb_icao", 6000, 0, 0, 0]
    a2 = [100.99, 34.00, 136.00, 6000, 180, 180, 0, -600, None, "adsb_icao", 6000, 0, 0, 0]
    b1 = [1900.48, 34.001, 136.00, 7000, 160, 0, 0, 700, None, "adsb_icao", 7000, 0, 0, 0]
    b2 = [1960.0, 34.01, 136.00, 8000, 200, 0, 0, 600, None, "adsb_icao", 8000, 0, 0, 0]
    # int(base+100.99)=base+100, int(base+1900.48)=base+1900 -> integer diff 1800 fires the arm.
    segs = routes.trace_segments(_synthetic([a1, a2, b1, b2]), DAY)
    assert len(segs) == 2


def test_slow_gap_persisted_grid_below_1800_does_not_split():
    # Persisted ts differ by 1799 even though wall-clock is ~1799.4 s: sub-threshold on the integer
    # grid, so the arm holds and it stays one segment.
    a = [100.50, 34.00, 136.00, 6000, 200, 180, 0, -600, None, "adsb_icao", 6000, 0, 0, 0]
    b = [1899.90, 34.001, 136.00, 7000, 160, 0, 0, 700, None, "adsb_icao", 7000, 0, 0, 0]
    # int(base+100.50)=base+100, int(base+1899.90)=base+1899 -> integer diff 1799 (< SLOW_GAP_S).
    segs = routes.trace_segments(_synthetic([a, b]), DAY)
    assert len(segs) == 1


def test_slow_gap_asymmetric_altitude_splits():
    # One boundary alt above the cruise ceiling, one below: the arm keys off the LOWER alt (min), so
    # a descent that bottoms out low still splits. A min->max regression would keep it fused.
    a1 = [0.0, 34.00, 136.00, 5000, 200, 180, 0, -600, None, "adsb_icao", 5000, 0, 0, 0]
    a2 = [60.0, 34.00, 136.00, 5000, 180, 180, 0, -600, None, "adsb_icao", 5000, 0, 0, 0]
    b1 = [60.0 + 2100, 34.001, 136.00, 20000, 160, 0, 0, 700, None, "adsb_icao", 20000, 0, 0, 0]
    b2 = [60.0 + 2100 + 60, 34.01, 136.00, 21000, 200, 0, 0, 600, None, "adsb_icao", 21000, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a1, a2, b1, b2]), DAY)
    assert len(segs) == 2


def test_slow_gap_one_null_altitude_splits():
    # One boundary altitude missing (coalesces to 99999) but the other is low: min() still sees the
    # low side, so a single None must not veto the split.
    a1 = [0.0, 34.00, 136.00, 5000, 200, 180, 0, -600, None, "adsb_icao", 5000, 0, 0, 0]
    a2 = [60.0, 34.00, 136.00, 5000, 180, 180, 0, -600, None, "adsb_icao", 5000, 0, 0, 0]
    b1 = [60.0 + 2100, 34.001, 136.00, "", 160, 0, 0, 700, None, "adsb_icao", None, 0, 0, 0]
    b2 = [60.0 + 2100 + 60, 34.01, 136.00, "", 200, 0, 0, 600, None, "adsb_icao", None, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a1, a2, b1, b2]), DAY)
    assert len(segs) == 2


def test_slow_gap_ceiling_boundary_exact():
    # Both boundary alts exactly at the cruise ceiling: the guard is strict (<), so 9843.0 does not
    # count as below and the coverage void stays one segment.
    a = [0.0, 34.00, 136.00, 9843.0, 200, 180, 0, 0, None, "adsb_icao", 9843, 0, 0, 0]
    b = [2100.0, 34.001, 136.00, 9843.0, 180, 180, 0, 0, None, "adsb_icao", 9843, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([a, b]), DAY)
    assert len(segs) == 1


def test_slow_gap_speed_line_boundary():
    # Straddle the 100 km/h line over an 1800 s gap (deltas checked against _haversine_km at write
    # time): 0.4485 deg ~ 99.7 km/h splits (stopped); 0.4520 deg ~ 100.5 km/h does not (flying).
    slow = [[0.0, 34.00, 136.00, 5000, 200, 180, 0, 0, None, "adsb_icao", 5000, 0, 0, 0],
            [60.0, 34.00, 136.00, 5000, 200, 180, 0, 0, None, "adsb_icao", 5000, 0, 0, 0],
            [1860.0, 34.00 + 0.4485, 136.00, 6000, 200, 180, 0, 0, None, "adsb_icao", 6000, 0, 0, 0],
            [1920.0, 34.00 + 0.4485, 136.00, 6000, 200, 180, 0, 0, None, "adsb_icao", 6000, 0, 0, 0]]
    fast = [[0.0, 34.00, 136.00, 5000, 200, 180, 0, 0, None, "adsb_icao", 5000, 0, 0, 0],
            [60.0, 34.00, 136.00, 5000, 200, 180, 0, 0, None, "adsb_icao", 5000, 0, 0, 0],
            [1860.0, 34.00 + 0.4520, 136.00, 6000, 200, 180, 0, 0, None, "adsb_icao", 6000, 0, 0, 0],
            [1920.0, 34.00 + 0.4520, 136.00, 6000, 200, 180, 0, 0, None, "adsb_icao", 6000, 0, 0, 0]]
    assert len(routes.trace_segments(_synthetic(slow), DAY)) == 2
    assert len(routes.trace_segments(_synthetic(fast), DAY)) == 1


def test_slow_gap_both_ground_splits_parked_cluster():
    # A >= 30-min ground silence also fragments a parked stretch: all-ground pieces drop at the
    # keep-filter, so only the segment that opens on the ground fix and lifts off survives.
    g0 = [0.0, 34.000, 136.000, "ground", 5, 90, 0, 0, None, "adsb_icao", 0, 0, 0, 0]
    g1 = [60.0, 34.000, 136.000, "ground", 5, 90, 0, 0, None, "adsb_icao", 0, 0, 0, 0]
    g2 = [2160.0, 34.001, 136.000, "ground", 5, 90, 0, 0, None, "adsb_icao", 0, 0, 0, 0]
    a1 = [2220.0, 34.002, 136.000, 1500, 120, 0, 0, 800, None, "adsb_icao", 1500, 0, 0, 0]
    a2 = [2280.0, 34.010, 136.000, 2500, 150, 0, 0, 700, None, "adsb_icao", 2500, 0, 0, 0]
    segs = routes.trace_segments(_synthetic([g0, g1, g2, a1, a2]), DAY)
    assert len(segs) == 1
    assert segs[0]["seg_start"] == 1782345600 + 2160  # opens on the post-gap ground fix
    assert segs[0]["first_on_ground"] is True
    assert segs[0]["num_fixes"] == 3
