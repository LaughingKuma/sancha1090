from __future__ import annotations

import json
from pathlib import Path

import pytest

import include.adsblol_routes as routes
from conftest import ARRIVE, BASE, DAY, _fix, _gnd

FIXTURE = Path(__file__).parent / "fixtures" / "trace_full_a61c53_2026-06-25.json"
REPO = Path(__file__).resolve().parents[1]
ROUTES_MODEL = REPO / "dbt" / "sancha1090" / "models" / "silver" / "int_flight_routes_adsblol.sql"
LEGS_MODEL = REPO / "dbt" / "sancha1090" / "models" / "silver" / "int_flight_legs_opensky.sql"
RECONCILE_GATES = REPO / "dbt" / "sancha1090" / "macros" / "reconcile_gates.sql"


def _doc():
    return json.loads(FIXTURE.read_text())


def _synthetic(points, icao="abc123", base=BASE):
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


def test_route_targets_targets_every_reconciled_flight():
    # SQL lower()s icao24; the fake returns rows already lowered as the driver would.
    fake = _FakeClient([("a61c53",), ("abc123",), ("a61c53",)])
    out = routes.route_targets(DAY, client=fake)
    sql = fake.seen["sql"]
    # Overlap predicate targets both departure-day and arrival-day flights.
    assert "toDate(start_time) = %(day)s" in sql
    assert "toDate(end_time) = %(day)s" in sql
    # rung 1: endpoint-NULL-only targeting starved fct_flight_path once SWIM began resolving
    # O/D pre-departure — every reconciled flight is a trace target now.
    assert "origin_icao" not in sql and "dest_icao" not in sql
    assert "icao24 IS NOT NULL" in sql
    assert fake.seen["parameters"] == {"day": DAY.isoformat()}
    assert out == ["a61c53", "abc123"]  # deduped + sorted


def test_rooftop_cohort_filters_junk_lowercases_and_sorts():
    fake = _FakeClient([("b61c53",), ("a61c53",), ("zzzzzz",), ("b61c53",)])
    out = routes.rooftop_cohort(DAY, client=fake)
    sql = fake.seen["sql"]
    assert "FROM bronze.adsb_states" in sql
    assert "capture_date = %(day)s" in sql
    assert "hex IS NOT NULL" in sql
    assert "match(lower(hex), '^[0-9a-f]{6}$')" in sql
    assert fake.seen["parameters"] == {"day": DAY.isoformat()}
    # "zzzzzz" isn't a hex digit string -> the Python-side belt drops it even though the fake
    # client bypasses the SQL match() filter; the rest dedup + sort.
    assert out == ["a61c53", "b61c53"]


class _CountingClient(_FakeClient):
    def __init__(self, rows):
        super().__init__(rows)
        self.closed = 0

    def close(self):
        self.closed += 1


def test_release_targets_is_one_query_over_all_three_sources():
    fake = _FakeClient([("a61c53",)])
    routes.release_targets(DAY, client=fake)
    sql = fake.seen["sql"]
    assert "bronze.adsb_states" in sql
    assert "bronze.opensky_states" in sql
    assert "fct_flights_reconciled" in sql
    assert sql.count("UNION ALL") == 2
    # Both days bound once each: the release lane extracts D's tar for D and D+1 targets.
    assert fake.seen["parameters"] == {"d0": "2026-06-25", "d1": "2026-06-26"}
    assert "capture_date IN (%(d0)s, %(d1)s)" in sql
    assert "toDate(snapshot_time) IN (%(d0)s, %(d1)s)" in sql
    assert "toDate(start_time) IN (%(d0)s, %(d1)s)" in sql
    assert "toDate(end_time) IN (%(d0)s, %(d1)s)" in sql
    assert "match(h, '^[0-9a-f]{6}$')" in sql


def test_release_targets_leaves_a_caller_supplied_client_open():
    fake = _CountingClient([("a61c53",)])
    assert routes.release_targets(DAY, client=fake) == ["a61c53"]
    assert fake.closed == 0


def test_release_targets_dedups_sorts_and_drops_junk():
    fake = _FakeClient([("b61c53",), ("a61c53",), ("zzzzzz",), ("b61c53",)])
    # The fake bypasses the SQL match(), so this pins the Python-side belt on the union's output.
    assert routes.release_targets(DAY, client=fake) == ["a61c53", "b61c53"]


def test_rooftop_cohort_closes_its_own_client(monkeypatch):
    closed = []

    class _ClosingClient(_FakeClient):
        def close(self):
            closed.append(True)

    fake = _ClosingClient([("a61c53",)])
    import include.clickhouse as ch_mod
    monkeypatch.setattr(ch_mod, "ch_client", lambda: fake)
    assert routes.rooftop_cohort(DAY) == ["a61c53"]
    assert closed == [True]


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
    # 1800: the arm evaluates on that integer grid (matching SLOW_GAP_SQL), so it splits.
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
    assert segs[0]["seg_start"] == BASE + 2160  # opens on the post-gap ground fix
    assert segs[0]["first_on_ground"] is True
    assert segs[0]["num_fixes"] == 3


@pytest.mark.parametrize("model", [ROUTES_MODEL, LEGS_MODEL], ids=["adsblol", "opensky_legs"])
def test_snap_tier_order_by_pins_airport_type(model):
    # #213A/#214: one snap tier across lanes -- each CTE orders through snap_order(), so a revert in either
    # one fails here, not only in the heavier dbt-side test; the tier itself is pinned once on the macro.
    sql = model.read_text()
    origin_sql, dest_sql = sql.split("dest_snap as (", 1)
    for name, block in (("origin_snap", origin_sql), ("dest_snap", dest_sql)):
        assert block.count("order by {{ snap_order(") == 1, f"{name} no longer orders through snap_order()"
    assert "a.iata != ''" not in sql
    assert "a.scheduled_service and" not in sql
    macros = RECONCILE_GATES.read_text()
    assert "not in ('heliport', 'seaplane_base')" in macros, "real_airfield() macro definition moved/changed"
    snap_order = macros[macros.index("macro snap_order("):]
    assert "if({{ real_airfield('a.airport_type') }}" in snap_order, "snap_order() no longer tiers on real_airfield()"
    assert "{{ var('snap_iata_pref_km') }}, 0, 1)," in snap_order, "snap_order() tier threshold missing"
    # a.icao closes the window: distance alone isn't a total order (ATUA/AYUA share coordinates).
    assert ", a.icao\n" in snap_order, "snap_order() tiebreak missing"


def test_parked_dwell_splits_without_ground_flag():
    # #229: 40 min parked at 300 ft baro under 10 kt with the ground bit never set reads as ground, so the
    # air->ground break ends the arrival and the takeoff trim opens the departure on the run's last fix.
    parked = [_fix(180.0 + k * 300, 34.02, 300, 5) for k in range(9)]  # 180 s .. 2580 s = 40 min
    depart = [_fix(2700.0, 34.02, 300, 90), _fix(2760.0, 34.03, 1500, 150),
              _fix(2820.0, 34.05, 4000, 220)]
    doc = _synthetic(ARRIVE + parked + depart)
    segs = routes.trace_segments(doc, DAY)
    assert len(segs) == 2
    assert segs[0]["seg_end"] == BASE + 120 and segs[0]["last_on_ground"] is False
    assert segs[0]["num_fixes"] == 3
    assert segs[1]["seg_start"] == BASE + 2580 and segs[1]["first_on_ground"] is True
    assert segs[1]["first_alt_ft"] == 300  # the baro alt is kept; only the flag is derived
    assert segs[1]["last_alt_ft"] == 4000 and segs[1]["num_fixes"] == 4
    pts = routes.trace_paths(doc, DAY, segs)
    assert {p["seg_start"] for p in pts} == {s["seg_start"] for s in segs}
    for s in segs:
        assert sum(1 for p in pts if p["seg_start"] == s["seg_start"]) == s["num_fixes"]
    # The parked fixes before the last one belong to an all-ground piece and are never persisted.
    assert [p["ts"] - BASE for p in pts if p["on_ground"]] == [2580]


def test_parked_dwell_shorter_than_floor_stays_one_segment():
    # 25 min under 10 kt (< DWELL_S): a long taxi queue, not a stop.
    parked = [_fix(180.0 + k * 300, 34.02, 300, 5) for k in range(6)]  # 180 s .. 1680 s = 25 min
    depart = [_fix(1740.0, 34.02, 300, 90), _fix(1800.0, 34.03, 1500, 150)]
    segs = routes.trace_segments(_synthetic(ARRIVE + parked + depart), DAY)
    assert len(segs) == 1 and segs[0]["num_fixes"] == 11


def test_cruise_hold_never_reads_as_ground():
    # The #107 cruise-hold class: 45 min at 5,000 ft with gs >= 80 throughout must stay one segment.
    pts = [_fix(k * 300.0, 34.0 + (k % 2) * 0.05, 5000, 80 + (k % 3) * 10) for k in range(10)]
    segs = routes.trace_segments(_synthetic(pts), DAY)
    assert len(segs) == 1 and segs[0]["num_fixes"] == 10


def test_taxi_out_stays_one_segment_and_opens_on_ground():
    # A ground-bit-unset taxi-out before takeoff: 40 min under 10 kt then climb. No airborne fix precedes
    # the run, so nothing splits; the segment opens on the run's last (derived) ground fix at the field.
    taxi = [_fix(k * 300.0, 34.02, 300, 5) for k in range(9)]  # 0 .. 2400 s = 40 min
    depart = [_fix(2460.0, 34.02, 300, 90), _fix(2520.0, 34.03, 1500, 150), _fix(2580.0, 34.05, 4000, 220)]
    segs = routes.trace_segments(_synthetic(taxi + depart), DAY)
    assert len(segs) == 1
    assert segs[0]["first_on_ground"] is True and segs[0]["num_fixes"] == 4
    assert segs[0]["seg_start"] == BASE + 2400


def test_dwell_altitude_guard_keeps_balloons_and_hovers_airborne():
    # HBAL124 at 53,000 ft under 30 kt for hours, JA10AP hovering at 1,700 ft: not on the ground.
    for alt in (53000, 1700, routes.LOW_FIX_ALT_FT):
        before = [_fix(0.0, 34.00, alt, 160), _fix(60.0, 34.01, alt, 140)]
        slow = [_fix(120.0 + k * 300, 34.02, alt, 5) for k in range(9)]
        after = [_fix(2880.0, 34.03, alt, 150), _fix(2940.0, 34.05, alt, 220)]
        assert len(routes.trace_segments(_synthetic(before + slow + after), DAY)) == 1


def test_dwell_none_speed_or_altitude_fails_open():
    # A fix missing gs or alt breaks the run (mirrors DWELL_SQL's coalesce(..., false)), so two 20-min
    # slow halves around it never add up to a dwell.
    arrive = [_fix(0.0, 34.00, 2500, 160), _fix(60.0, 34.01, 300, 60)]
    half1 = [_fix(120.0 + k * 300, 34.02, 300, 5) for k in range(5)]     # 120 .. 1320
    hole = [_fix(1500.0, 34.02, 300, ""), _fix(1560.0, 34.02, "", 5)]
    half2 = [_fix(1620.0 + k * 300, 34.02, 300, 5) for k in range(5)]    # 1620 .. 2820
    depart = [_fix(2880.0, 34.03, 1500, 150), _fix(2940.0, 34.05, 4000, 220)]
    assert len(routes.trace_segments(_synthetic(arrive + half1 + hole + half2 + depart), DAY)) == 1


def test_stationary_segment_drops_once_read_as_ground():
    # A "flight" that never exceeds 30 kt (JAL0000 tugs, MH691 taxiing with the bit unset) is all
    # ground after the dwell read and falls at the keep-filter instead of voting a same-airport route.
    pts = [_fix(k * 300.0, 34.02 + k * 0.0001, 100, 12) for k in range(8)]
    assert routes.trace_segments(_synthetic(pts), DAY) == []


def test_dwell_persisted_grid_boundary():
    # Run length is measured on int(ts) like the slow-gap arm: 1799.6 s wall-clock that truncates to 1800
    # splits; a run that truncates to 1799 does not.
    def run(first, last):
        arrive = [_fix(0.0, 34.00, 2500, 160), _fix(60.0, 34.01, 300, 60)]
        slow = [_fix(first, 34.02, 300, 5), _fix(first + 900, 34.02, 300, 5), _fix(last, 34.02, 300, 5)]
        depart = [_fix(last + 60, 34.03, 1500, 150), _fix(last + 120, 34.05, 4000, 220)]
        return len(routes.trace_segments(_synthetic(arrive + slow + depart), DAY))
    assert run(100.9, 1900.5) == 2   # int diff 1800
    assert run(100.5, 1899.9) == 1   # int diff 1799


def test_segments_and_paths_come_from_one_group_walk(monkeypatch):
    # Both tables key off the same group boundaries; a second walk loop is how they drifted before.
    doc = _doc()
    calls = []
    real = routes._iter_groups

    def spy(points, base):
        calls.append(len(points))
        return real(points, base)

    monkeypatch.setattr(routes, "_iter_groups", spy)
    segs, pts = routes.trace_rows(doc, DAY)
    assert len(calls) == 1
    assert segs and {p["seg_start"] for p in pts} == {s["seg_start"] for s in segs}
    assert routes.trace_segments(doc, DAY) == segs
    assert routes.trace_paths(doc, DAY, segs) == pts
    assert routes.trace_paths(doc, DAY, segs[:1]) == [p for p in pts if p["seg_start"] == segs[0]["seg_start"]]


def test_leading_ground_run_trims_departure_to_last_ground_fix():
    # DAL121 shape: the segment opens on a flagged-ground fix, parks 40 min under coverage, then departs.
    # The last ground fix before rotation opens the departure; the all-ground piece before it drops.
    parked = [_gnd(k * 300.0, 34.02) for k in range(9)]                     # 0 .. 2400 s
    depart = [_gnd(2460.0, 34.02, gs=20), _fix(2520.0, 34.03, 1500, 150), _fix(2580.0, 34.05, 4000, 220)]
    doc = _synthetic(parked + depart)
    segs = routes.trace_segments(doc, DAY)
    assert len(segs) == 1
    assert segs[0]["seg_start"] == BASE + 2460 and segs[0]["first_on_ground"] is True
    assert segs[0]["num_fixes"] == 3
    pts = routes.trace_paths(doc, DAY, segs)
    assert [p["ts"] - BASE for p in pts] == [2460, 2520, 2580]


def test_takeoff_roll_with_a_ground_bit_flicker_drops_whole():
    # A ground-bit flicker inside a second the grid reads as ground, mid-roll (a27c78 06-27) or in the trim second
    # (71be22 06-24), must not keep the roll: only the run's last fix persists, as the departure's first fix.
    parked = [_gnd(k * 300.0, 34.02, gs=5) for k in range(9)]                 # 0 .. 2400 s
    roll = [_gnd(2460.0, 34.02, gs=60), _gnd(2500.2, 34.021, gs=80), _fix(2500.4, 34.021, 25, 80),
            _gnd(2500.9, 34.021, gs=85), _gnd(2520.2, 34.022, gs=88), _fix(2520.4, 34.022, 25, 88),
            _gnd(2520.9, 34.022, gs=90)]
    depart = [_fix(2580.0, 34.03, 1500, 150), _fix(2640.0, 34.05, 4000, 220)]
    doc = _synthetic(parked + roll + depart)
    segs = routes.trace_segments(doc, DAY)
    assert [(s["seg_start"] - BASE, s["num_fixes"], s["first_on_ground"]) for s in segs] == [(2520, 3, True)]
    pts = routes.trace_paths(doc, DAY, segs)
    assert [(p["ts"] - BASE, p["seg_start"] - BASE, p["on_ground"]) for p in pts] == [
        (2520, 2520, True), (2580, 2520, False), (2640, 2520, False)]


def test_takeoff_roll_drop_leaves_the_arrival_its_fix_in_the_landing_second():
    # The trimmed run starts at the landing second, whose earlier fix is the arrival's last airborne one (899000
    # 06-04): flagging it ground would move it into the dropped roll and shorten the arrival by a fix.
    landing = [_fix(180.2, 34.02, 275, 101), _gnd(180.5, 34.02, gs=99), _fix(180.7, 34.02, 275, 99),
               _gnd(180.8, 34.02, gs=99), _gnd(240.0, 34.02, gs=40)]
    parked = [_gnd(480.0 + k * 300.0, 34.02) for k in range(8)]                 # 480 .. 2580 s
    depart = [_fix(2640.0, 34.03, 1500, 150), _fix(2700.0, 34.05, 4000, 220)]
    doc = _synthetic(ARRIVE + landing + parked + depart)
    segs = routes.trace_segments(doc, DAY)
    assert [(s["seg_start"] - BASE, s["seg_end"] - BASE, s["num_fixes"], s["last_on_ground"]) for s in segs] == [
        (0, 180, 4, False), (2580, 2700, 3, False)]
    pts = routes.trace_paths(doc, DAY, segs)
    assert [(p["ts"] - BASE, p["seg_start"] - BASE, p["on_ground"]) for p in pts] == [
        (0, 0, False), (60, 0, False), (120, 0, False), (180, 0, False),
        (2580, 2580, True), (2640, 2580, False), (2700, 2580, False)]


def test_turnaround_trim_keeps_arrival_and_opens_departure_at_takeoff():
    # Arrival, landing ground fix, 45 min parked, departure: the arrival ends airborne as before, the
    # parked piece drops, the departure opens on the last ground fix.
    arrive = [_fix(0.0, 34.00, 2500, 160), _fix(60.0, 34.01, 1200, 140)]
    parked = [_gnd(120.0 + k * 300, 34.02) for k in range(10)]               # 120 .. 2820 s
    depart = [_fix(2880.0, 34.03, 1500, 150), _fix(2940.0, 34.05, 4000, 220)]
    segs = routes.trace_segments(_synthetic(arrive + parked + depart), DAY)
    assert [(s["seg_start"] - BASE, s["num_fixes"]) for s in segs] == [(0, 2), (2820, 3)]
    assert segs[0]["last_on_ground"] is False and segs[1]["first_on_ground"] is True


def test_ground_run_under_floor_keeps_the_whole_taxi_out():
    # 25 min flagged ground then takeoff: under DWELL_S, so the segment still opens at the first ground fix.
    parked = [_gnd(k * 300.0, 34.02) for k in range(6)]                     # 0 .. 1500 s
    depart = [_fix(1560.0, 34.03, 1500, 150), _fix(1620.0, 34.05, 4000, 220)]
    segs = routes.trace_segments(_synthetic(parked + depart), DAY)
    assert len(segs) == 1 and segs[0]["seg_start"] == BASE and segs[0]["num_fixes"] == 8


def test_ground_run_ends_at_a_turnaround_sized_silence():
    # 40 min parked, a 35-min silence (the slow-gap arm splits there), 5 min ground, takeoff: the run that
    # counts is the 5-min head, so nothing trims and the departure opens after the silence as it does today.
    parked = [_gnd(k * 300.0, 34.02) for k in range(9)]                     # 0 .. 2400 s
    head = [_gnd(2400.0 + routes.SLOW_GAP_S + 300 * k, 34.02) for k in range(2)]  # 4200, 4500
    depart = [_fix(4560.0, 34.03, 1500, 150), _fix(4620.0, 34.05, 4000, 220)]
    segs = routes.trace_segments(_synthetic(parked + head + depart), DAY)
    assert len(segs) == 1 and segs[0]["seg_start"] == BASE + 4200 and segs[0]["num_fixes"] == 4


def test_dwell_derived_ground_run_also_trims():
    # The ground-bit-unset turnaround (AAL61 at KDFW): the dwell read makes the run ground, and the trim
    # then opens the departure at its last fix rather than at the stand's first fix.
    parked = [_fix(180.0 + k * 300, 34.02, 300, 5) for k in range(9)]       # 180 .. 2580 s
    depart = [_fix(2700.0, 34.02, 300, 90), _fix(2760.0, 34.03, 1500, 150)]
    segs = routes.trace_segments(_synthetic(ARRIVE + parked + depart), DAY)
    assert [(s["seg_start"] - BASE, s["num_fixes"]) for s in segs] == [(0, 3), (2580, 3)]
    assert segs[1]["first_on_ground"] is True and segs[1]["first_alt_ft"] == 300


def test_ground_run_at_trace_end_never_trims():
    parked = [_gnd(k * 300.0, 34.02) for k in range(9)]
    assert routes.trace_segments(_synthetic([_fix(0.0, 34.00, 2500, 160)] + parked), DAY) == []


def test_dwell_run_ends_at_a_turnaround_sized_silence():
    # Two sub-floor slow heads either side of a parked silence (71c208 2026-09-01) stay two runs: the walk
    # drops the all-ground tail at the silence, which the backfill selector could never see across.
    def run(gap):
        arrive = [_fix(0.0, 34.00, 2500, 160), _fix(60.0, 34.01, 1200, 140)]
        tail = [_fix(120.0 + k * 100, 34.02, 75, 8) for k in range(6)]
        head = [_fix(620.0 + gap + k * 100, 34.02, 75, 4) for k in range(6)]
        depart = [_fix(620.0 + gap + 700, 34.03, 1500, 150), _fix(620.0 + gap + 760, 34.05, 4000, 220)]
        return routes.trace_segments(_synthetic(arrive + tail + head + depart), DAY)
    segs = run(routes.SLOW_GAP_S)
    assert [(s["num_fixes"], s["last_on_ground"], s["first_on_ground"]) for s in segs] == [
        (8, False, False), (8, False, False)]
    segs = run(routes.SLOW_GAP_S - 1)  # one run: the tail drops, the trim opens the departure on its last fix
    assert [(s["num_fixes"], s["last_on_ground"], s["first_on_ground"]) for s in segs] == [
        (2, False, False), (3, False, True)]


def test_same_second_interrupt_reads_as_the_persisted_dwell():
    # Two sub-floor slow runs split by one 80 kt fix that shares its whole second with a later slow fix: the
    # RMT keeps the slow one, so the selector sees a 40-min dwell and the walk must read the same run or never converge.
    first = [_fix(180.0 + k * 100, 34.02, 300, 5) for k in range(10)]           # 180 .. 1080, 15 min
    interrupt = [_fix(1180.2, 34.02, 300, 80), _fix(1180.7, 34.02, 300, 5)]
    second = [_fix(1280.0 + k * 100, 34.02, 300, 5) for k in range(16)]         # 1280 .. 2780, 25 min
    depart = [_fix(2900.0, 34.03, 1500, 150), _fix(2960.0, 34.05, 4000, 220)]
    segs = routes.trace_segments(_synthetic(ARRIVE + first + interrupt + second + depart), DAY)
    assert len(segs) == 2
    assert segs[1]["seg_start"] == BASE + 2780 and segs[1]["first_on_ground"] is True
