from __future__ import annotations

import logging
from datetime import datetime, timezone

from dags import ingest_adsb as ia


def _r(stream, ok, rotation_end_ts):
    return {"filename": f"{stream}.f", "stream": stream, "ok": ok,
            "rotation_end_ts": rotation_end_ts}


def test_summarize_counts_landed_and_failed_by_stream():
    results = [
        _r("adsb_state", True, "2026-05-29T01:00:00Z"),
        _r("adsb_state", False, "2026-05-29T01:00:00Z"),
        _r("beast_raw", True, "2026-05-29T01:00:00Z"),
        None,  # a mapped instance the scheduler skipped
    ]
    s = ia.summarize_results(results)
    assert s["landed"] == 2
    assert s["failed"] == 1
    assert s["adsb_landed"] == 1
    assert s["beast_landed"] == 1


def test_adsb_landed_count_only_counts_successful_adsb():
    results = [
        _r("adsb_state", True, "2026-05-29T01:00:00Z"),
        _r("adsb_state", False, "2026-05-29T01:00:00Z"),
        _r("beast_raw", True, "2026-05-29T01:00:00Z"),
    ]
    assert ia.adsb_landed_count(results) == 1


def test_maybe_log_stale_errors_when_newest_adsb_over_2h_old(caplog):
    now = datetime(2026, 5, 29, 4, 0, 0, tzinfo=timezone.utc)
    results = [_r("adsb_state", True, "2026-05-29T01:00:00Z")]  # 3h behind → stale
    with caplog.at_level(logging.ERROR):
        stale = ia.maybe_log_stale(results, now=now, logger=logging.getLogger("t"))
    assert stale is True
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_maybe_log_stale_quiet_when_fresh(caplog):
    now = datetime(2026, 5, 29, 1, 30, 0, tzinfo=timezone.utc)
    results = [_r("adsb_state", True, "2026-05-29T01:00:00Z")]  # 30m behind → fresh
    with caplog.at_level(logging.ERROR):
        stale = ia.maybe_log_stale(results, now=now, logger=logging.getLogger("t"))
    assert stale is False
    assert not any(r.levelno == logging.ERROR for r in caplog.records)


def test_maybe_log_stale_quiet_when_no_adsb_landed():
    now = datetime(2026, 5, 29, 4, 0, 0, tzinfo=timezone.utc)
    results = [_r("beast_raw", True, "2026-05-29T01:00:00Z")]
    assert ia.maybe_log_stale(results, now=now, logger=logging.getLogger("t")) is False


def test_maybe_log_stale_alerts_from_manifest_when_run_landed_nothing(caplog):
    # The silent-producer case: no current-run adsb results, but the manifest's newest is >2h old.
    now = datetime(2026, 5, 29, 4, 0, 0, tzinfo=timezone.utc)
    manifest_newest = datetime(2026, 5, 29, 1, 0, 0, tzinfo=timezone.utc)  # 3h behind
    with caplog.at_level(logging.ERROR):
        stale = ia.maybe_log_stale([], now=now, logger=logging.getLogger("t"),
                                   manifest_newest=manifest_newest)
    assert stale is True
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_maybe_log_stale_quiet_when_manifest_fresh_and_no_results():
    now = datetime(2026, 5, 29, 1, 30, 0, tzinfo=timezone.utc)
    manifest_newest = datetime(2026, 5, 29, 1, 0, 0, tzinfo=timezone.utc)  # 30m behind
    assert ia.maybe_log_stale([], now=now, logger=logging.getLogger("t"),
                              manifest_newest=manifest_newest) is False
