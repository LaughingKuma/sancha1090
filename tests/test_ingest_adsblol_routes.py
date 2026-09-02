from __future__ import annotations

from datetime import date

import pendulum
import pytest

from include import adsblol_release as rel
from include import clickhouse as ch


def _task(dagbag):
    return dagbag.dags["ingest_adsblol_routes"].get_task("load_to_clickhouse").python_callable


def _land_task(dagbag):
    return dagbag.dags["ingest_adsblol_routes"].get_task("land_releases").python_callable


def _wait_task(dagbag):
    return dagbag.dags["ingest_adsblol_routes"].get_task("wait_for_release").python_callable


END_DT = pendulum.datetime(2026, 7, 11, 4, 0, tz="UTC")
SWEEP = [date(2026, 7, 7), date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10)]


def test_segments_failure_raises(dagbag, monkeypatch):
    monkeypatch.setattr(ch, "load_adsblol_segments_pending_to_ch", lambda: {"ok": False, "error": "boom"})
    monkeypatch.setattr(ch, "load_adsblol_paths_pending_to_ch", lambda: {"ok": True})
    with pytest.raises(RuntimeError, match="segments"):
        _task(dagbag)({})


def test_paths_only_failure_raises(dagbag, monkeypatch):
    monkeypatch.setattr(ch, "load_adsblol_segments_pending_to_ch", lambda: {"ok": True, "rows": 5})
    monkeypatch.setattr(ch, "load_adsblol_paths_pending_to_ch", lambda: {"ok": False, "error": "boom"})
    with pytest.raises(RuntimeError, match="paths"):
        _task(dagbag)({})


def test_both_ok_returns_both(dagbag, monkeypatch):
    monkeypatch.setattr(ch, "load_adsblol_segments_pending_to_ch", lambda: {"ok": True})
    monkeypatch.setattr(ch, "load_adsblol_paths_pending_to_ch", lambda: {"ok": True})
    assert _task(dagbag)({}) == {"segments": {"ok": True}, "paths": {"ok": True}}


def test_landing_failure_still_loads_pending_then_raises(dagbag, monkeypatch):
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
    with pytest.raises(RuntimeError, match="release landing failed"):
        _task(dagbag)(None)
    assert called == ["segments", "paths"]


def _fake_land(monkeypatch, fn):
    monkeypatch.setattr(rel, "land_day_if_due", fn)


def test_land_task_sweeps_four_days_oldest_first(dagbag, monkeypatch):
    calls = []

    def fake(day, *, force=False, kinds=None, min_traces=None):
        calls.append({"day": day, "force": force, "kinds": kinds, "min_traces": min_traces})
        return {"status": "landed"}

    _fake_land(monkeypatch, fake)
    result = _land_task(dagbag)(data_interval_end=END_DT)
    assert [c["day"] for c in calls] == SWEEP
    assert all(c["force"] is False and c["kinds"] == rel.KIND_SUFFIXES
               and c["min_traces"] == rel.MIN_TRACES for c in calls)
    assert sorted(result) == [d.isoformat() for d in SWEEP]


def test_land_task_conf_overrides(dagbag, monkeypatch):
    calls = []

    def fake(day, *, force=False, kinds=None, min_traces=None):
        calls.append({"day": day, "force": force, "kinds": kinds, "min_traces": min_traces})
        return {"status": "landed"}

    _fake_land(monkeypatch, fake)

    class _Run:
        conf = {"trace_days": ["2026-08-13", "2026-08-11"], "force": True,
                "kind": "staging-0", "min_traces": 500}
        run_after = END_DT

    _land_task(dagbag)(data_interval_end=END_DT, dag_run=_Run())
    assert [c["day"] for c in calls] == [date(2026, 8, 11), date(2026, 8, 13)]
    assert all(c["force"] is True and c["kinds"] == ("staging-0",)
               and c["min_traces"] == 500 for c in calls)

    calls.clear()

    class _Zero:
        conf = {"trace_days": ["2026-08-13"], "min_traces": 0}
        run_after = END_DT

    # An explicit 0 disables the floor on purpose; `or MIN_TRACES` would have swallowed it.
    _land_task(dagbag)(data_interval_end=END_DT, dag_run=_Zero())
    assert [c["min_traces"] for c in calls] == [0]


def test_land_task_rejects_an_unknown_kind(dagbag, monkeypatch):
    _fake_land(monkeypatch, lambda *_a, **_kw: {"status": "landed"})

    class _Run:
        conf = {"kind": "nightly-9"}
        run_after = END_DT

    with pytest.raises(ValueError, match="kind must be one of"):
        _land_task(dagbag)(data_interval_end=END_DT, dag_run=_Run())


def test_land_task_one_day_failing_still_sweeps_rest_then_raises(dagbag, monkeypatch):
    calls = []

    def fake(day, **_kw):
        calls.append(day)
        if day == date(2026, 7, 9):
            raise RuntimeError("boom")
        return {"status": "landed"}

    _fake_land(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="2026-07-09"):
        _land_task(dagbag)(data_interval_end=END_DT)
    assert calls == SWEEP


def test_land_task_reds_on_error_pairs(dagbag, monkeypatch):
    def fake(day, **_kw):
        return {"status": "landed", "errors": 3 if day == date(2026, 7, 8) else 0}

    _fake_land(monkeypatch, fake)
    with pytest.raises(RuntimeError, match=r"2026-07-08 \(3 error pair\(s\)\)"):
        _land_task(dagbag)(data_interval_end=END_DT)


def test_land_task_marker_reread_with_old_errors_does_not_red(dagbag, monkeypatch):
    # The error red belongs to the tick that landed the day; the marker re-reads on the next three
    # sweep ticks carry the same errors count and must stay green.
    def fake(day, **_kw):
        return {"status": "already_landed" if day == date(2026, 7, 8) else "landed",
                "errors": 3 if day == date(2026, 7, 8) else 0}

    _fake_land(monkeypatch, fake)
    result = _land_task(dagbag)(data_interval_end=END_DT)
    assert result["2026-07-08"]["status"] == "already_landed"


def test_land_task_unpublished_older_day_warns_but_passes(dagbag, monkeypatch):
    def fake(day, **_kw):
        return {"status": "unpublished" if day == date(2026, 7, 7) else "landed"}

    _fake_land(monkeypatch, fake)
    result = _land_task(dagbag)(data_interval_end=END_DT)
    assert result["2026-07-07"]["status"] == "unpublished"


def test_wait_task_pokes_newest_day_with_kinds(dagbag, monkeypatch):
    seen = []

    class _Ref:
        tag = "v2026.07.10-planes-readsb-staging-0"

    monkeypatch.setattr(rel, "find_release",
                        lambda day, kinds: seen.append((day, kinds)) or _Ref())

    class _Run:
        conf = {"kind": "staging-0"}
        run_after = END_DT

    out = _wait_task(dagbag)(data_interval_end=END_DT, dag_run=_Run())
    assert seen == [(date(2026, 7, 10), ("staging-0",))]
    assert out.is_done is True and out.xcom_value == _Ref.tag


def test_wait_task_not_done_when_unpublished(dagbag, monkeypatch):
    monkeypatch.setattr(rel, "find_release", lambda _day, _kinds: None)
    out = _wait_task(dagbag)(data_interval_end=END_DT)
    assert out.is_done is False and out.xcom_value is None


def test_wait_task_not_done_when_github_hiccups(dagbag, monkeypatch):
    def _boom(_day, _kinds):
        raise rel.ProbeFailed("HEAD kept failing")

    monkeypatch.setattr(rel, "find_release", _boom)
    out = _wait_task(dagbag)(data_interval_end=END_DT)
    assert out.is_done is False


def test_wait_precedes_land(dagbag):
    dag = dagbag.dags["ingest_adsblol_routes"]
    assert dag.get_task("wait_for_release").downstream_task_ids == {"land_releases"}


def test_land_feeds_load_to_clickhouse(dagbag):
    dag = dagbag.dags["ingest_adsblol_routes"]
    assert dag.get_task("land_releases").downstream_task_ids == {"load_to_clickhouse"}
