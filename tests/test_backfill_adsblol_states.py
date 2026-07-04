from __future__ import annotations

from datetime import date

import pytest
import sqlalchemy as sa

import scripts.backfill_adsblol_states as bas


def _fresh_engine():
    eng = sa.create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE ingestion_manifest ("
            " object_uri TEXT PRIMARY KEY,"
            " loaded_at TIMESTAMP,"
            " snapshot_min INTEGER,"
            " snapshot_max INTEGER,"
            " row_count INTEGER,"
            " ch_loaded_at TIMESTAMP,"
            " archived_at TIMESTAMP"
            ")"
        ))
    return eng


def _insert(eng, uri, *, ch_loaded_at=None):
    with eng.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO ingestion_manifest (object_uri, ch_loaded_at) VALUES (:uri, :ch)"
        ), {"uri": uri, "ch": ch_loaded_at})


def test_quality_gate_fails_below_min_traces():
    with pytest.raises(RuntimeError, match="quality gate"):
        bas._quality_gate(members=9_999, corrupt=0, min_traces=10_000)


def test_quality_gate_fails_over_corrupt_ratio():
    with pytest.raises(RuntimeError, match="quality gate"):
        bas._quality_gate(members=10_000, corrupt=101, min_traces=10_000)  # 1.01%


def test_quality_gate_passes_clean_day():
    bas._quality_gate(members=50_000, corrupt=10, min_traces=10_000)


def test_day_range_ascending_when_start_before_end():
    days = list(bas._day_range(date(2026, 5, 18), date(2026, 5, 22)))
    assert days == [date(2026, 5, 18), date(2026, 5, 19), date(2026, 5, 20),
                     date(2026, 5, 21), date(2026, 5, 22)]


def test_day_range_descending_when_start_after_end():
    days = list(bas._day_range(date(2026, 4, 30), date(2026, 4, 28)))
    assert days == [date(2026, 4, 30), date(2026, 4, 29), date(2026, 4, 28)]


def test_day_range_single_day_when_equal():
    days = list(bas._day_range(date(2026, 5, 18), date(2026, 5, 18)))
    assert days == [date(2026, 5, 18)]


def test_manifest_status_missing_when_no_row():
    eng = _fresh_engine()
    assert bas._manifest_status("s3://bucket/key", engine=eng) == "missing"


def test_manifest_status_pending_when_row_not_ch_loaded():
    eng = _fresh_engine()
    _insert(eng, "s3://bucket/key")
    assert bas._manifest_status("s3://bucket/key", engine=eng) == "pending"


def test_manifest_status_ch_loaded_when_marker_set():
    eng = _fresh_engine()
    _insert(eng, "s3://bucket/key", ch_loaded_at="2026-07-01T00:00:00")
    assert bas._manifest_status("s3://bucket/key", engine=eng) == "ch_loaded"


def test_consecutive_missing_stops_at_threshold():
    counter = bas._MissingCounter(stop_after=3)
    assert counter.record_missing() is False
    assert counter.record_missing() is False
    assert counter.record_missing() is True
    assert counter.record_missing() is True


def test_consecutive_missing_resets_on_a_found_day():
    counter = bas._MissingCounter(stop_after=3)
    counter.record_missing()
    counter.record_missing()
    counter.record_found()
    assert counter.record_missing() is False  # 1/3 again, not 3/3
    assert counter.record_missing() is False  # 2/3


def test_run_returns_failure_exit_code_even_when_floor_hit_after_an_earlier_failure(monkeypatch):
    monkeypatch.setattr(bas, "analytics_engine", lambda: None)
    monkeypatch.setattr(bas, "get_bucket", lambda: "bucket")
    monkeypatch.setattr(bas, "_manifest_status", lambda *_args, **_kwargs: "missing")

    calls = {"n": 0}

    def fake_open_release(_day):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return None  # every day after the first reports no release, tripping the floor

    monkeypatch.setattr(bas, "_open_release", fake_open_release)

    rc = bas.run(date(2026, 5, 1), date(2026, 5, 5), min_traces=10_000, stop_after_missing=1, dry_run=False)
    assert rc == 1
