from __future__ import annotations

import sys
from datetime import date

import pytest

import scripts.backfill_flight_paths as bfp


def _fail(*_a, **_kw):
    raise AssertionError("must not be called in dry-run")


def test_dry_run_counts_pending_pairs_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53", "abc123"])
    monkeypatch.setattr(bfp.routes, "run_daily", _fail)
    monkeypatch.setattr(bfp.ledger, "record_attempts", _fail)
    seen = []

    def fake_filter(pairs, engine):
        seen.append((pairs, engine))
        return pairs[:1]  # pretend one of the two is already ledgered

    monkeypatch.setattr(bfp.ledger, "filter_unattempted", fake_filter)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 25), workers=2, dry_run=True)
    assert rc == 0
    assert seen == [([("a61c53", "2026-06-25"), ("abc123", "2026-06-25")], "ENGINE")]


def test_dry_run_prints_per_day_counts_and_grand_total(capsys, monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    cohorts = {"2026-06-25": ["a61c53", "abc123"], "2026-06-26": ["def456"]}
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda day: cohorts[day.isoformat()])
    # Day 1: 1 of 2 pairs pending; day 2: both pending.
    monkeypatch.setattr(bfp.ledger, "filter_unattempted",
                        lambda pairs, _engine: pairs[:1] if len(pairs) == 2 else pairs)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 26), workers=2, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-06-25: 2 cohort hexes, 1 pending" in out
    assert "2026-06-26: 1 cohort hexes, 1 pending" in out
    assert "TOTAL pending across era: 2" in out


def test_live_run_passes_cohort_targets_and_workers_through(monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])
    calls = []

    def fake_run_daily(day, targets=None, *, engine=None, workers=1, progress=None):  # noqa: ARG001 (progress kw-bound)
        calls.append({"day": day, "targets": targets, "engine": engine, "workers": workers})
        return {"fetched": 1, "landed": 1, "missing": 0, "errors": 0}

    monkeypatch.setattr(bfp.routes, "run_daily", fake_run_daily)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 25), workers=5, dry_run=False)
    assert rc == 0
    assert calls == [{"day": date(2026, 6, 25), "targets": ["a61c53"], "engine": "ENGINE", "workers": 5}]


def test_live_run_reports_progress_heartbeat(capsys, monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])

    def fake_run_daily(_day, targets=None, *, engine=None, workers=1, progress=None):  # noqa: ARG001
        if progress is not None:
            progress(2, 2)  # final pair always heartbeats regardless of the every-100 cadence
        return {"fetched": 2, "landed": 2, "missing": 0, "errors": 0}

    monkeypatch.setattr(bfp.routes, "run_daily", fake_run_daily)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 25), workers=2, dry_run=False)
    assert rc == 0
    assert "2026-06-25: 2/2 fetched" in capsys.readouterr().out


def test_live_run_day_failure_is_recorded_and_other_days_still_run(monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])
    days_seen = []

    def fake_run_daily(day, targets=None, *, engine=None, workers=1, progress=None):  # noqa: ARG001
        days_seen.append(day)
        if day == date(2026, 6, 25):
            raise RuntimeError("boom")
        return {"fetched": 1, "landed": 1, "missing": 0, "errors": 0}

    monkeypatch.setattr(bfp.routes, "run_daily", fake_run_daily)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 26), workers=2, dry_run=False)
    assert rc == 1
    assert days_seen == [date(2026, 6, 25), date(2026, 6, 26)]  # day 2 still attempted


def test_live_run_partial_errors_without_exception_still_fails_run(monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])
    days_seen = []

    def fake_run_daily(day, targets=None, *, engine=None, workers=1, progress=None):  # noqa: ARG001
        days_seen.append(day)
        errored = day == date(2026, 6, 25)
        return {"fetched": 1, "landed": 0 if errored else 1, "missing": 0, "errors": 1 if errored else 0}

    monkeypatch.setattr(bfp.routes, "run_daily", fake_run_daily)

    # No exception raised -- run_daily returns normally with errors > 0 -- but the day must still
    # be treated as a failure, and day 2 must still run.
    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 26), workers=2, dry_run=False)
    assert rc == 1
    assert days_seen == [date(2026, 6, 25), date(2026, 6, 26)]


def test_live_run_partial_errors_named_in_summary(capsys, monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])
    monkeypatch.setattr(bfp.routes, "run_daily",
                        lambda *_a, **_kw: {"fetched": 1, "landed": 0, "missing": 0, "errors": 1})

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 25), workers=2, dry_run=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "2026-06-25 (partial: 1 pair(s) errored)" in out
    # The operator must know errors are NOT abandoned after N attempts — they stay retryable
    # indefinitely (ledger redesign: error outcomes skip max_attempts, cooldown 4 min).
    assert "4-minute cooldown" in out
    assert "never abandons them" in out


def test_main_default_end_is_yesterday_utc(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["backfill_flight_paths.py"])
    captured = {}
    monkeypatch.setattr(bfp, "run",
                        lambda start, end, workers, dry_run:
                        captured.update(start=start, end=end, workers=workers, dry_run=dry_run) or 0)

    class _FixedDatetime(bfp.datetime):
        @classmethod
        def now(cls, tz=None):
            return bfp.datetime(2026, 7, 11, 3, 0, tzinfo=tz)

    monkeypatch.setattr(bfp, "datetime", _FixedDatetime)

    rc = bfp.main()
    assert rc == 0
    assert captured["start"] == bfp.ROOFTOP_ERA_START
    assert captured["end"] == date(2026, 7, 10)
    assert captured["workers"] == 2
    assert captured["dry_run"] is False


def test_main_parses_explicit_args(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "backfill_flight_paths.py",
        "--start", "2026-06-01", "--end", "2026-06-02", "--workers", "4", "--dry-run",
    ])
    captured = {}
    monkeypatch.setattr(bfp, "run",
                        lambda start, end, workers, dry_run:
                        captured.update(start=start, end=end, workers=workers, dry_run=dry_run) or 0)

    rc = bfp.main()
    assert rc == 0
    assert captured == {
        "start": date(2026, 6, 1), "end": date(2026, 6, 2), "workers": 4, "dry_run": True,
    }


def test_workers_arg_bounded_1_to_4(monkeypatch):
    # Out-of-range rejected at the argparse layer (0 and 5 invalid; 1..4 is the politeness cap).
    monkeypatch.setattr(sys, "argv", ["backfill_flight_paths.py", "--workers", "0"])
    with pytest.raises(SystemExit):
        bfp.main()
    monkeypatch.setattr(sys, "argv", ["backfill_flight_paths.py", "--workers", "5"])
    with pytest.raises(SystemExit):
        bfp.main()
