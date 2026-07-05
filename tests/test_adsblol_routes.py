from __future__ import annotations

import gzip as _gzip
import io
import json
import urllib.error
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


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_fetch_trace_decompresses_gzip_and_builds_sharded_url():
    seen = {}

    def opener(req, timeout):  # noqa: ARG001 (timeout is keyword-bound in fetch_trace's call)
        seen["url"] = req.full_url
        return _FakeResp(_gzip.compress(b'{"icao": "a61c53", "timestamp": 1, "trace": []}'))

    doc = routes.fetch_trace(DAY, "a61c53", opener=opener)
    assert doc["icao"] == "a61c53"
    assert seen["url"] == "https://globe.adsb.lol/globe_history/2026/06/25/traces/53/trace_full_a61c53.json"


def test_fetch_trace_404_means_no_trace():
    def opener(req, timeout):  # noqa: ARG001 (timeout is keyword-bound in fetch_trace's call)
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    assert routes.fetch_trace(DAY, "deadbe", opener=opener) is None


def test_fetch_trace_raises_after_retries(monkeypatch):
    monkeypatch.setattr(routes.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def opener(_req, timeout):  # noqa: ARG001 (timeout is keyword-bound in fetch_trace's call)
        calls["n"] += 1
        raise urllib.error.URLError("boom")

    import pytest
    with pytest.raises(RuntimeError):
        routes.fetch_trace(DAY, "a61c53", opener=opener)
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


def test_livemap_routes_sql_unions_adsblol():
    import include.flight_routes as fr

    sql = fr._routes_sql()
    assert "int_flight_chains_adsblol" in sql
    assert "fact_flights" in sql
