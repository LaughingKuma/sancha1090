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
    monkeypatch.setattr(bfp, "land_release_day", _fail)
    monkeypatch.setattr(bfp.ledger, "record_attempts", _fail)
    seen = []

    def fake_filter(pairs, engine):
        seen.append((pairs, engine))
        return pairs[:1]  # pretend one of the two is already ledgered

    monkeypatch.setattr(bfp.ledger, "filter_unattempted", fake_filter)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 25), dry_run=True)
    assert rc == 0
    assert seen == [([("a61c53", "2026-06-25"), ("abc123", "2026-06-25")], "ENGINE")]


def test_dry_run_prints_per_day_counts_and_grand_total(capsys, monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    cohorts = {"2026-06-25": ["a61c53", "abc123"], "2026-06-26": ["def456"]}
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda day: cohorts[day.isoformat()])
    # Day 1: 1 of 2 pairs pending; day 2: both pending.
    monkeypatch.setattr(bfp.ledger, "filter_unattempted",
                        lambda pairs, _engine: pairs[:1] if len(pairs) == 2 else pairs)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 26), dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-06-25: 2 cohort hexes, 1 pending" in out
    assert "2026-06-26: 1 cohort hexes, 1 pending" in out
    assert "TOTAL pending across era: 2" in out


def test_live_run_passes_cohort_targets_through(monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])
    calls = []

    def fake_land(day, targets=None, *, engine=None, progress=None):  # noqa: ARG001 (progress kw-bound)
        calls.append({"day": day, "targets": targets, "engine": engine})
        return {"fetched": 1, "landed": 1, "missing": 0, "errors": 0}

    monkeypatch.setattr(bfp, "land_release_day", fake_land)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 25), dry_run=False)
    assert rc == 0
    assert calls == [{"day": date(2026, 6, 25), "targets": ["a61c53"], "engine": "ENGINE"}]


def test_live_run_reports_progress_heartbeat(capsys, monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])

    def fake_land(_day, targets=None, *, engine=None, progress=None):  # noqa: ARG001
        if progress is not None:
            progress(20_000)
        return {"fetched": 2, "landed": 2, "missing": 0, "errors": 0}

    monkeypatch.setattr(bfp, "land_release_day", fake_land)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 25), dry_run=False)
    assert rc == 0
    assert "2026-06-25: 20000 members scanned" in capsys.readouterr().out


def test_live_run_day_failure_is_recorded_and_other_days_still_run(monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])
    days_seen = []

    def fake_land(day, targets=None, *, engine=None, progress=None):  # noqa: ARG001
        days_seen.append(day)
        if day == date(2026, 6, 25):
            raise RuntimeError("boom")
        return {"fetched": 1, "landed": 1, "missing": 0, "errors": 0}

    monkeypatch.setattr(bfp, "land_release_day", fake_land)

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 26), dry_run=False)
    assert rc == 1
    assert days_seen == [date(2026, 6, 25), date(2026, 6, 26)]  # day 2 still attempted


def test_live_run_partial_errors_without_exception_still_fails_run(monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])
    days_seen = []

    def fake_land(day, targets=None, *, engine=None, progress=None):  # noqa: ARG001
        days_seen.append(day)
        errored = day == date(2026, 6, 25)
        return {"fetched": 1, "landed": 0 if errored else 1, "missing": 0, "errors": 1 if errored else 0}

    monkeypatch.setattr(bfp, "land_release_day", fake_land)

    # No exception raised -- land_release_day returns normally with errors > 0 -- but the day must
    # still be treated as a failure, and day 2 must still run.
    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 26), dry_run=False)
    assert rc == 1
    assert days_seen == [date(2026, 6, 25), date(2026, 6, 26)]


def test_live_run_partial_errors_named_in_summary(capsys, monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "rooftop_cohort", lambda _day: ["a61c53"])
    monkeypatch.setattr(bfp, "land_release_day",
                        lambda *_a, **_kw: {"fetched": 1, "landed": 0, "missing": 0, "errors": 1})

    rc = bfp.run(date(2026, 6, 25), date(2026, 6, 25), dry_run=False)
    out = capsys.readouterr().out
    assert rc == 1
    assert "2026-06-25 (partial: 1 pair(s) errored)" in out
    # The operator must know what a rerun does now: only landed pairs are permanently skipped.
    assert "errored and missing pairs re-propose on any rerun" in out
    assert "landed pairs never do" in out


def test_main_default_end_is_yesterday_utc(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["backfill_flight_paths.py"])
    captured = {}
    monkeypatch.setattr(bfp, "run",
                        lambda start, end, dry_run, cohort:
                        captured.update(start=start, end=end, dry_run=dry_run, cohort=cohort) or 0)

    class _FixedDatetime(bfp.datetime):
        @classmethod
        def now(cls, tz=None):
            return bfp.datetime(2026, 7, 11, 3, 0, tzinfo=tz)

    monkeypatch.setattr(bfp, "datetime", _FixedDatetime)

    rc = bfp.main()
    assert rc == 0
    assert captured["start"] == bfp.ROOFTOP_ERA_START
    assert captured["end"] == date(2026, 7, 10)
    assert captured["dry_run"] is False
    assert captured["cohort"] == "rooftop"


def test_main_parses_explicit_args(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "backfill_flight_paths.py", "--start", "2026-06-01", "--end", "2026-06-02", "--dry-run",
    ])
    captured = {}
    monkeypatch.setattr(bfp, "run",
                        lambda start, end, dry_run, cohort:
                        captured.update(start=start, end=end, dry_run=dry_run, cohort=cohort) or 0)

    rc = bfp.main()
    assert rc == 0
    assert captured == {
        "start": date(2026, 6, 1), "end": date(2026, 6, 2), "dry_run": True, "cohort": "rooftop",
    }


def test_reconciled_dry_run_counts_own_day_release_targets(monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "release_targets", lambda _day: ["a61c53"])
    monkeypatch.setattr(bfp, "land_release_day", _fail)
    seen = []
    monkeypatch.setattr(bfp.ledger, "filter_unattempted",
                        lambda pairs, _engine: seen.append(pairs) or pairs)
    rc = bfp.run(date(2026, 7, 6), date(2026, 7, 6), dry_run=True, cohort="reconciled")
    assert rc == 0
    # No day-1 halo any more: day-1's own extraction set already covers this day's hexes.
    assert seen == [[("a61c53", "2026-07-06")]]


def test_reconciled_live_run_uses_release_targets(monkeypatch):
    monkeypatch.setattr(bfp, "analytics_engine", lambda: "ENGINE")
    monkeypatch.setattr(bfp.routes, "release_targets", lambda _day: ["a61c53", "abc123"])
    calls = []

    def fake_land(day, targets=None, *, engine=None, progress=None):  # noqa: ARG001
        calls.append({"day": day, "targets": targets})
        return {"fetched": 2, "landed": 2, "missing": 0, "errors": 0}

    monkeypatch.setattr(bfp, "land_release_day", fake_land)
    rc = bfp.run(date(2026, 7, 6), date(2026, 7, 6), dry_run=False, cohort="reconciled")
    assert rc == 0
    assert calls == [{"day": date(2026, 7, 6), "targets": ["a61c53", "abc123"]}]


def test_main_passes_cohort_through(monkeypatch):
    monkeypatch.setattr(sys, "argv", [
        "backfill_flight_paths.py", "--start", "2026-07-06", "--end", "2026-07-07",
        "--cohort", "reconciled",
    ])
    captured = {}
    monkeypatch.setattr(bfp, "run",
                        lambda _start, _end, _dry_run, cohort:
                        captured.update(cohort=cohort) or 0)
    assert bfp.main() == 0
    assert captured["cohort"] == "reconciled"


def test_reconciled_cohort_requires_explicit_start(monkeypatch):
    # No --start + --cohort reconciled must not silently inherit the rooftop-era default —
    # that would stream ~8 weeks of multi-GB release tarballs.
    monkeypatch.setattr(sys, "argv", ["backfill_flight_paths.py", "--cohort", "reconciled"])
    with pytest.raises(SystemExit):
        bfp.main()
