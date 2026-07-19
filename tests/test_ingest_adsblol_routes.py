from __future__ import annotations

from datetime import date

import pendulum
import pytest

from include import adsblol_routes as routes
from include import clickhouse as ch


def _task(dagbag):
    return dagbag.dags["ingest_adsblol_routes"].get_task("load_to_clickhouse").python_callable


def _cohort_task(dagbag):
    return dagbag.dags["ingest_adsblol_routes"].get_task("cohort_fetch_and_land").python_callable


def test_segments_failure_raises(dagbag, monkeypatch):
    monkeypatch.setattr(ch, "load_adsblol_segments_pending_to_ch", lambda: {"ok": False, "error": "boom"})
    monkeypatch.setattr(ch, "load_adsblol_paths_pending_to_ch", lambda: {"ok": True})
    with pytest.raises(RuntimeError, match="segments"):
        _task(dagbag)({}, {})


def test_paths_only_failure_raises(dagbag, monkeypatch):
    monkeypatch.setattr(ch, "load_adsblol_segments_pending_to_ch", lambda: {"ok": True, "rows": 5})
    monkeypatch.setattr(ch, "load_adsblol_paths_pending_to_ch", lambda: {"ok": False, "error": "boom"})
    with pytest.raises(RuntimeError, match="paths"):
        _task(dagbag)({}, {})


def test_both_ok_returns_both(dagbag, monkeypatch):
    monkeypatch.setattr(ch, "load_adsblol_segments_pending_to_ch", lambda: {"ok": True})
    monkeypatch.setattr(ch, "load_adsblol_paths_pending_to_ch", lambda: {"ok": True})
    result = _task(dagbag)({}, {})
    assert result == {"segments": {"ok": True}, "paths": {"ok": True}}


def test_fetch_failure_still_loads_pending_then_raises(dagbag, monkeypatch):
    called = []
    monkeypatch.setattr(
        ch,
        "load_adsblol_segments_pending_to_ch",
        lambda: called.append("segments") or {"ok": True},
    )
    monkeypatch.setattr(
        ch,
        "load_adsblol_paths_pending_to_ch",
        lambda: called.append("paths") or {"ok": True},
    )
    with pytest.raises(RuntimeError, match="fetch lanes failed"):
        _task(dagbag)(None, {})
    assert called == ["segments", "paths"]


def test_cohort_task_calls_run_daily_with_cohort_targets_and_workers_2(dagbag, monkeypatch):
    monkeypatch.setattr(routes, "rooftop_cohort", lambda _day: ["a61c53", "abc123"])
    calls = []

    def fake_run_daily(
        day,
        targets=None,
        workers=1,
        include_error_retries=False,
        include_missing_retries=False,
        raise_on_errors=False,
    ):
        calls.append({
            "day": day,
            "targets": targets,
            "workers": workers,
            "include_error_retries": include_error_retries,
            "include_missing_retries": include_missing_retries,
            "raise_on_errors": raise_on_errors,
        })
        return {"fetched": 2}

    monkeypatch.setattr(routes, "run_daily", fake_run_daily)

    end_dt = pendulum.datetime(2026, 7, 11, 3, 0, tz="UTC")
    result = _cohort_task(dagbag)(data_interval_end=end_dt)
    assert result == {"fetched": 2}
    # data_interval_end - 1 day = the trace day the nightly run targets.
    assert calls == [{
        "day": date(2026, 7, 10),
        "targets": ["a61c53", "abc123"],
        "workers": 2,
        "include_error_retries": True,
        "include_missing_retries": True,
        "raise_on_errors": True,
    }]


def test_cohort_task_precedes_fetch_and_land(dagbag):
    dag = dagbag.dags["ingest_adsblol_routes"]
    assert dag.get_task("cohort_fetch_and_land").downstream_task_ids == {
        "fetch_and_land",
        "load_to_clickhouse",
    }


def test_fetch_and_land_still_feeds_load_to_clickhouse(dagbag):
    dag = dagbag.dags["ingest_adsblol_routes"]
    assert dag.get_task("fetch_and_land").downstream_task_ids == {"load_to_clickhouse"}


def _fetch_task(dagbag):
    return dagbag.dags["ingest_adsblol_routes"].get_task("fetch_and_land").python_callable


def test_fetch_task_sweeps_four_days_oldest_first(dagbag, monkeypatch):
    calls = []

    def fake_run_daily(day, targets=None, _workers=1, _include_error_retries=False,
                       raise_on_errors=False):
        calls.append({"day": day, "targets": targets, "raise_on_errors": raise_on_errors})
        return {"fetched": 1}

    monkeypatch.setattr(routes, "run_daily", fake_run_daily)
    end_dt = pendulum.datetime(2026, 7, 11, 3, 0, tz="UTC")
    result = _fetch_task(dagbag)(data_interval_end=end_dt)
    # Late-reconciliation sweep: the flights lane's authoritative D-2 pull lands 14:30 UTC,
    # after this DAG's 03:00 tick, so a day needs a fourth shot; the ledger dedups re-proposals.
    assert [c["day"] for c in calls] == [date(2026, 7, 7), date(2026, 7, 8),
                                         date(2026, 7, 9), date(2026, 7, 10)]
    assert all(c["targets"] is None and c["raise_on_errors"] for c in calls)
    assert result == {"2026-07-07": {"fetched": 1}, "2026-07-08": {"fetched": 1},
                      "2026-07-09": {"fetched": 1}, "2026-07-10": {"fetched": 1}}


def test_fetch_task_one_day_failing_still_sweeps_rest_then_raises(dagbag, monkeypatch):
    calls = []

    def fake_run_daily(day, **_kw):
        calls.append(day)
        if day == date(2026, 7, 9):
            raise RuntimeError("boom")
        return {"fetched": 1}

    monkeypatch.setattr(routes, "run_daily", fake_run_daily)
    end_dt = pendulum.datetime(2026, 7, 11, 3, 0, tz="UTC")
    with pytest.raises(RuntimeError, match="2026-07-09"):
        _fetch_task(dagbag)(data_interval_end=end_dt)
    assert calls == [date(2026, 7, 7), date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10)]
