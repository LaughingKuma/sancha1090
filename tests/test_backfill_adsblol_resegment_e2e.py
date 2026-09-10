from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import include.adsblol_routes as routes
import scripts.backfill_adsblol_resegment as bar
from conftest import ARRIVE, BASE, DAY, _fix, _gnd

# Live-ClickHouse proof that the selectors pick exactly the hex-days the new walk writes differently and go
# quiet once re-landed; the old walk is the same code with DWELL_S out of reach (both rules key on it).

TABLE = "bronze.p229_e2e_paths"
DDL = Path(__file__).resolve().parents[1] / "clickhouse" / "sql" / "01_bronze.sql"
FIXTURE = Path(__file__).parent / "fixtures" / "trace_full_a61c53_2026-06-25.json"
COLS = ["icao24", "seg_start", "ts", "lat", "lon", "alt_ft", "on_ground", "gs_kt", "track_deg",
        "trace_day", "source", "ingested_at", "committed_at"]


@pytest.fixture()
def scratch_ch(ch):
    ch.command(f"DROP TABLE IF EXISTS {TABLE}")
    # The real DDL, renamed: the RMT key is what collapses same-second fixes, so the fixture must share it.
    ddl = re.search(r"CREATE TABLE IF NOT EXISTS bronze\.adsblol_flight_paths\b.*?;", DDL.read_text(), re.S)
    assert ddl is not None, "bronze.adsblol_flight_paths DDL not found in clickhouse/sql/01_bronze.sql"
    ch.command(ddl.group(0).replace("bronze.adsblol_flight_paths", TABLE, 1))
    try:
        yield ch
    finally:
        ch.command(f"DROP TABLE IF EXISTS {TABLE}")


def _cases() -> dict[str, dict]:
    depart = [_fix(3000.0, 34.03, 1500, 150), _fix(3060.0, 34.05, 4000, 220), _fix(3120.0, 34.08, 8000, 280)]
    silence = 2400.0 + routes.SLOW_GAP_S
    cases = {
        # dwell read + trim
        "parked_dwell": ARRIVE + [_fix(180.0 + k * 300, 34.02, 300, 5) for k in range(9)] + depart,
        # trim only (DAL121 shape)
        "leading_ground_run": [_gnd(k * 300.0, 34.02) for k in range(9)] + depart,
        # the old walk dropped the 40-min piece before the silence; the 5-min head is under the floor: no change
        "dropped_piece_short_head": [_gnd(k * 300.0, 34.02) for k in range(9)]
        + [_gnd(silence + k * 150, 34.02) for k in range(3)]
        + [_fix(silence + 400, 34.03, 1500, 150), _fix(silence + 460, 34.05, 4000, 220)],
        # same, but the head after the silence is itself over the floor: trims
        "dropped_piece_long_head": [_gnd(k * 300.0, 34.02) for k in range(9)]
        + [_gnd(silence + k * 300, 34.02) for k in range(8)]
        + [_fix(silence + 2160, 34.03, 1500, 150), _fix(silence + 2220, 34.05, 4000, 220)],
        # the last ground fix and the first airborne fix share a whole second: the RMT keeps one of them
        "same_second_rotation": [_gnd(k * 300.0, 34.02) for k in range(9)]
        + [_gnd(2460.2, 34.02, gs=25), _fix(2460.8, 34.021, 200, 140), _fix(2520.0, 34.03, 1500, 150),
           _fix(2580.0, 34.05, 4000, 220)],
        # a slow head 20 min into a stand-opening segment, 40 min of flagged ground parked before a 1 h silence:
        # the run must not span the silence back into the piece the old walk dropped (unchanged, unselected)
        "dropped_piece_slow_head": [_gnd(k * 300.0, 34.02) for k in range(9)]
        + [_gnd(6000.0 + k * 300, 34.02) for k in range(3)] + [_fix(6900.0, 34.02, 350, 0.1)]
        + [_gnd(7200.0, 34.02), _fix(7260.0, 34.03, 1500, 150), _fix(7320.0, 34.05, 4000, 220)],
        # 71c208 shape: two sub-floor slow heads across a parked silence stay two sub-floor runs (unchanged)
        "cross_gap_dwell": ARRIVE + [_fix(180.0 + k * 100, 34.02, 75, 8) for k in range(6)]
        + [_fix(7980.0 + k * 100, 34.02, 75, 4) for k in range(6)]
        + [_fix(8700.0, 34.03, 1500, 150), _fix(8760.0, 34.05, 4000, 220)],
        # never over 30 kt: kept by the old walk, dropped by the new one
        "stationary": [_fix(k * 300.0, 34.02 + k * 0.0001, 100, 12) for k in range(8)],
        # unchanged shapes
        "plain_flight": [_fix(k * 120.0, 34.0 + k * 0.05, 5000 + k * 500, 250) for k in range(12)],
        "short_turnaround": ARRIVE + [_gnd(180.0 + k * 300, 34.02) for k in range(4)]
        + [_fix(1500.0, 34.03, 1500, 150), _fix(1560.0, 34.05, 4000, 220)],
        "cruise_hold": [_fix(k * 300.0, 34.0 + (k % 2) * 0.05, 5000, 80 + (k % 3) * 10) for k in range(10)],
        # two sub-floor slow runs split by an 80 kt fix whose whole second the RMT hands to a later slow fix:
        # the persisted rows read as one 40-min dwell, so the walk must split it or the selector never quiets
        "same_second_interrupt": ARRIVE + [_fix(180.0 + k * 100, 34.02, 300, 5) for k in range(10)]
        + [_fix(1180.2, 34.02, 300, 80), _fix(1180.7, 34.02, 300, 5)]
        + [_fix(1280.0 + k * 100, 34.02, 300, 5) for k in range(16)]
        + [_fix(2900.0, 34.03, 1500, 150), _fix(2960.0, 34.05, 4000, 220)],
    }
    docs = {f"e2e{n:03d}": {"icao": f"e2e{n:03d}", "timestamp": BASE, "trace": pts}
            for n, pts in enumerate(cases.values())}
    docs["a61c53"] = json.loads(FIXTURE.read_text())
    return docs


def _walk(doc):
    segs = routes.trace_segments(doc, DAY)
    return segs, routes.trace_paths(doc, DAY, segs)


def _persisted(paths):
    # One row per whole second, last write wins: the (trace_day, icao24, ts) RMT key under FINAL.
    return {p["ts"]: tuple(p[c] for c in ("seg_start", "lat", "lon", "alt_ft", "on_ground", "gs_kt", "track_deg"))
            for p in paths}


def _insert(ch, paths):
    stamp = datetime(2026, 9, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    rows = [[p["icao24"],
             datetime.fromtimestamp(p["seg_start"], tz=timezone.utc).replace(tzinfo=None),
             datetime.fromtimestamp(p["ts"], tz=timezone.utc).replace(tzinfo=None),
             p["lat"], p["lon"], p["alt_ft"], p["on_ground"], p["gs_kt"], p["track_deg"],
             DAY, "adsblol", stamp, stamp] for p in paths]
    ch.insert(TABLE, rows, column_names=COLS)


def test_selectors_pick_exactly_the_changed_hex_days_then_go_quiet(scratch_ch, monkeypatch):
    docs = _cases()
    new = {h: _walk(d) for h, d in docs.items()}
    with monkeypatch.context() as m:
        m.setattr(routes, "DWELL_S", 10**9)
        old = {h: _walk(d) for h, d in docs.items()}
    changed = {(h, DAY.isoformat()) for h in docs
               if old[h][0] != new[h][0] or _persisted(old[h][1]) != _persisted(new[h][1])}
    assert {h for h, _ in changed} == {
        "e2e000", "e2e001", "e2e003", "e2e004", "e2e007", "e2e011", "a61c53"}, "fixture shapes drifted"

    for h in docs:
        _insert(scratch_ch, old[h][1])
    assert set(bar.affected_pairs(client=scratch_ch, table=TABLE)) == changed

    scratch_ch.command(f"TRUNCATE TABLE {TABLE}")
    for h in docs:
        _insert(scratch_ch, new[h][1])
    assert bar.affected_pairs(client=scratch_ch, table=TABLE) == []
