import collections
import importlib.util
import re
import time
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


def test_track_serves_rw_points_passthrough(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_fetch_track", lambda _icao: [[139.7, 35.6, 1765500000.0, "38000"]])
    j = TestClient(livemap.app).get("/track/abc123").json()
    assert j == {"hex": "abc123", "points": [[139.7, 35.6, 1765500000.0, "38000"]]}


def test_track_rw_failure_returns_empty_200(livemap, monkeypatch):
    def boom(_icao):
        raise RuntimeError("rw down")

    monkeypatch.setattr(livemap, "_fetch_track", boom)
    r = TestClient(livemap.app).get("/track/abc123")
    assert r.status_code == 200
    assert r.json() == {"hex": "abc123", "points": []}


def test_track_query_targets_mv_in_capture_order(livemap):
    assert "FROM mv_track_positions" in livemap.TRACK_QUERY
    assert "ORDER BY capture_ts" in livemap.TRACK_QUERY


def test_history_buffer_demoted_to_wake_window(livemap):
    # /history clamps to 120 s; with /track on RW the deque needs nothing more
    assert livemap._track_buf.maxlen == max(1, int(120 / livemap.POLL_SECONDS))


def test_history_still_filters_by_cutoff(livemap, monkeypatch):
    now = time.time()
    # fresh deque via monkeypatch — auto-restored, so the module-scoped fixture stays pristine
    buf = collections.deque(maxlen=livemap._track_buf.maxlen)
    buf.append((now - 200, [["aaa", 1.0, 2.0, now - 200, "1000"]]))
    buf.append((now - 5, [["aaa", 1.1, 2.1, now - 5, "1100"]]))
    monkeypatch.setattr(livemap, "_track_buf", buf)
    snaps = TestClient(livemap.app).get("/history", params={"s": 120}).json()["snapshots"]
    assert len(snaps) == 1
    assert snaps[0][0] == pytest.approx(now - 5)


def test_aircraft_query_serves_emergency_and_source_fields(livemap):
    # PR 1b-i: squawk drives the emergency banner/pulse; position_source drives the MLAT/ADS-B pill.
    # Assert against the SELECT projection so an incidental substring elsewhere can't pass this.
    m = re.search(r"SELECT(?P<select>.*?)FROM\s+mv_current_aircraft", livemap.QUERY, flags=re.I | re.S)
    assert m, "aircraft query shape changed: missing SELECT ... FROM mv_current_aircraft"
    select_list = m.group("select")
    assert re.search(r"\bsquawk\b", select_list, flags=re.I)
    assert re.search(r"\bposition_source\b", select_list, flags=re.I)
