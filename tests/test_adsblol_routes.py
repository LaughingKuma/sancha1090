from __future__ import annotations

import json
from datetime import date
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
