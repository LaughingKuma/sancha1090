from __future__ import annotations

import scripts.backfill_adsblol_resegment as bar
from include.adsblol_routes import SLOW_GAP_CEIL_FT, SLOW_GAP_S, SLOW_GAP_SPEED_KMH


def test_affected_sql_interpolates_task1_constants():
    sql = bar.AFFECTED_SQL
    assert str(SLOW_GAP_S) in sql           # 1800 epoch-second turnaround-sized gap
    assert str(SLOW_GAP_CEIL_FT) in sql     # 9843.0 ft cruise ceiling (CH alt_ft is feet)
    assert str(SLOW_GAP_SPEED_KMH) in sql   # 100 km/h implied cross-gap speed guard
    assert "adsblol_flight_paths FINAL" in sql
    assert "lagInFrame" in sql
    assert "2700" not in sql
    assert "984.0" not in sql
    # Expression-level parity with the Python _seg_break slow-gap arm on the persisted integer grid.
    assert f"ts - prev_ts >= {SLOW_GAP_S}" in sql
    assert f"least(coalesce(prev_alt_ft, 99999.), coalesce(alt_ft, 99999.)) < {SLOW_GAP_CEIL_FT}" in sql
    # Speed selection is _haversine_km term-for-term (R=6371.0, division form), NOT greatCircleDistance:
    # SQL must never select a pair the arm won't split, or the backfill's dry-run can't converge.
    assert "asin(sqrt(" in sql
    assert "6371.0" in sql
    assert "/ ((ts - prev_ts) / 3600.)" in sql
    assert f"3600.) < {SLOW_GAP_SPEED_KMH}" in sql
    assert "greatCircleDistance" not in sql


class _FakeQueryResult:
    def __init__(self, result_rows):
        self.result_rows = result_rows


class _FakeCHClient:
    # Records each lightweight DELETE (table, bound params) so tests can assert the supersede-cleanup.
    def __init__(self, sink, stale_rows=None, cleared=3):
        self._sink = sink
        self._stale = stale_rows or []
        self._cleared = cleared
        self.queries = []

    def command(self, sql, parameters=None):
        self._sink.append((sql.split()[2], parameters))  # "DELETE FROM <table> WHERE ..."
        return None

    def query(self, sql, **_kw):
        self.queries.append(sql)
        if "count()" in sql:
            return _FakeQueryResult([[self._cleared]])
        return _FakeQueryResult(list(self._stale))  # same canned rows regardless of {table}

    def close(self):
        pass


def _stub_flow(monkeypatch, pairs, *, drain_ok=True, run_daily_result=None, cleared=3,
               stale_rows=None):
    monkeypatch.setattr(bar, "affected_pairs", lambda: pairs)
    events = []
    deleted = []

    def fake_delete(ps, *_a, **_k):
        deleted.append(list(ps))
        events.append(("delete", tuple(ps)))
        return len(ps)

    monkeypatch.setattr(bar.ledger, "delete_attempts", fake_delete)
    fetched = []

    def fake_run_daily(day, targets=None, **_kw):
        fetched.append((day.isoformat(), tuple(targets)))
        events.append(("fetch", day.isoformat(), tuple(targets)))
        if run_daily_result is not None:
            return run_daily_result(day, list(targets))
        return {"fetched": len(targets) * 2, "landed": len(targets),
                "missing": 0, "errors": 0, "rows": len(targets), "path_rows": len(targets),
                "landed_hexes": sorted(targets)}

    monkeypatch.setattr(bar, "run_daily", fake_run_daily)
    loaded = []

    def fake_load(name):
        def _load(*_a, **_k):
            loaded.append(name)
            events.append(("load", name))
            return {"ch_loaded": 0, "files": 0, "ok": drain_ok}
        return _load

    monkeypatch.setattr(bar, "load_adsblol_segments_pending_to_ch", fake_load("seg"))
    monkeypatch.setattr(bar, "load_adsblol_paths_pending_to_ch", fake_load("path"))
    ch_deletes = []
    monkeypatch.setattr(bar, "ch_client",
                        lambda: _FakeCHClient(ch_deletes, stale_rows=stale_rows, cleared=cleared))
    return deleted, fetched, loaded, events, ch_deletes


def test_dry_run_is_default_and_mutates_nothing(monkeypatch, capsys):
    deleted, fetched, loaded, _events, ch_deletes = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25"), ("ffff01", "2026-06-25")])
    rc = bar.run(execute=False)
    assert rc == 0
    assert deleted == [] and fetched == [] and loaded == [] and ch_deletes == []
    out = capsys.readouterr().out
    assert "2 pairs" in out and "dry-run" in out


def test_execute_clears_ledger_then_refetches_then_loads(monkeypatch):
    # A restartable script must clear-then-refetch per day (never batch all deletes up front),
    # so a crash mid-run leaves at most one day's ledger rows in a gap.
    _deleted, _fetched, _loaded, events, ch_deletes = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25"), ("ffff01", "2026-06-25"),
                      ("a61c53", "2026-06-26")])
    rc = bar.run(execute=True, sleep=0.0)
    assert rc == 0
    assert events == [
        ("delete", (("a61c53", "2026-06-25"), ("ffff01", "2026-06-25"))),
        ("fetch", "2026-06-25", ("a61c53", "ffff01")),
        ("delete", (("a61c53", "2026-06-26"),)),
        ("fetch", "2026-06-26", ("a61c53",)),
        ("load", "seg"),
        ("load", "path"),
    ]
    # Supersede-deletes run only AFTER both loaders drained, per day, both bronze tables.
    assert [t for t, _p in ch_deletes] == [
        "bronze.adsblol_flight_segments", "bronze.adsblol_flight_paths",
        "bronze.adsblol_flight_segments", "bronze.adsblol_flight_paths",
    ]


def test_execute_prints_progress_heartbeat(monkeypatch, capsys):
    monkeypatch.setattr(bar, "affected_pairs", lambda: [("a61c53", "2026-06-25")])
    monkeypatch.setattr(bar.ledger, "delete_attempts", lambda ps, *_a, **_k: len(ps))

    def fake_run_daily(_day, _targets=None, progress=None, **_kw):
        # The final pair always heartbeats regardless of the every-100 cadence.
        if progress is not None:
            progress(2, 2)
        return {"fetched": 2, "landed": 1, "missing": 0, "errors": 0, "rows": 1, "path_rows": 1,
                "landed_hexes": ["a61c53"]}

    monkeypatch.setattr(bar, "run_daily", fake_run_daily)
    monkeypatch.setattr(bar, "load_adsblol_segments_pending_to_ch",
                        lambda *_a, **_k: {"ch_loaded": 0, "files": 0, "ok": True})
    monkeypatch.setattr(bar, "load_adsblol_paths_pending_to_ch",
                        lambda *_a, **_k: {"ch_loaded": 0, "files": 0, "ok": True})
    monkeypatch.setattr(bar, "ch_client", lambda: _FakeCHClient([]))

    bar.run(execute=True, sleep=0.0)
    assert "2026-06-25: 2/2 fetched" in capsys.readouterr().out


def test_workers_arg_validated_and_passed_through(monkeypatch):
    import pytest

    # Out-of-range rejected at the argparse layer (0 invalid, >8 invalid); default is 5.
    with pytest.raises(SystemExit):
        bar._parse_args(["--workers", "0"])
    with pytest.raises(SystemExit):
        bar._parse_args(["--workers", "9"])
    assert bar._parse_args([]).workers == 5

    seen = {}
    monkeypatch.setattr(bar, "affected_pairs", lambda: [("a61c53", "2026-06-25")])
    monkeypatch.setattr(bar.ledger, "delete_attempts", lambda ps, *_a, **_k: len(ps))

    def fake_run_daily(_day, workers=1, **_kw):
        seen["workers"] = workers
        return {"fetched": 2, "landed": 1, "missing": 0, "errors": 0, "rows": 1, "path_rows": 1,
                "landed_hexes": ["a61c53"]}

    monkeypatch.setattr(bar, "run_daily", fake_run_daily)
    monkeypatch.setattr(bar, "load_adsblol_segments_pending_to_ch",
                        lambda *_a, **_k: {"ch_loaded": 0, "files": 0, "ok": True})
    monkeypatch.setattr(bar, "load_adsblol_paths_pending_to_ch",
                        lambda *_a, **_k: {"ch_loaded": 0, "files": 0, "ok": True})
    monkeypatch.setattr(bar, "ch_client", lambda: _FakeCHClient([]))
    bar.run(execute=True, sleep=0.0, workers=7)
    assert seen["workers"] == 7


def test_days_limit_pilots_first_day_only(monkeypatch):
    _deleted, fetched, _loaded, _events, _ch = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25"), ("ffff01", "2026-06-26")])
    bar.run(execute=True, sleep=0.0, days_limit=1)
    assert fetched == [("2026-06-25", ("a61c53",))]


def test_execute_returns_nonzero_on_fetch_errors_but_still_drains_loaders(monkeypatch):
    monkeypatch.setattr(bar, "affected_pairs", lambda: [("a61c53", "2026-06-25")])
    monkeypatch.setattr(bar.ledger, "delete_attempts", lambda ps, *_a, **_k: len(ps))
    monkeypatch.setattr(bar, "run_daily", lambda _day, _targets=None, **_kw: {
        "fetched": 2, "landed": 0, "missing": 0, "errors": 2, "rows": 0, "path_rows": 0,
        "landed_hexes": []})
    loaded = []
    monkeypatch.setattr(bar, "load_adsblol_segments_pending_to_ch",
                        lambda *_a, **_k: loaded.append("seg") or {"ch_loaded": 0, "files": 0, "ok": True})
    monkeypatch.setattr(bar, "load_adsblol_paths_pending_to_ch",
                        lambda *_a, **_k: loaded.append("path") or {"ch_loaded": 0, "files": 0, "ok": True})
    monkeypatch.setattr(bar, "ch_client", lambda: _FakeCHClient([]))

    rc = bar.run(execute=True, sleep=0.0)
    assert rc == 1
    # The exit code signals re-run needed, but whatever landed must still drain to CH.
    assert loaded == ["seg", "path"]


def test_execute_returns_nonzero_when_drain_reports_not_ok(monkeypatch):
    # The loaders are best-effort and never raise (include/clickhouse.py's _safe guard), so a
    # failed drain only surfaces via ok=False — run() must fold that into the exit status too.
    monkeypatch.setattr(bar, "affected_pairs", lambda: [("a61c53", "2026-06-25")])
    monkeypatch.setattr(bar.ledger, "delete_attempts", lambda ps, *_a, **_k: len(ps))
    monkeypatch.setattr(bar, "run_daily", lambda _day, _targets=None, **_kw: {
        "fetched": 2, "landed": 1, "missing": 0, "errors": 0, "rows": 1, "path_rows": 1,
        "landed_hexes": ["a61c53"]})
    monkeypatch.setattr(bar, "load_adsblol_segments_pending_to_ch",
                        lambda *_a, **_k: {"ch_loaded": 0, "files": 0, "ok": False})
    monkeypatch.setattr(bar, "load_adsblol_paths_pending_to_ch",
                        lambda *_a, **_k: {"ch_loaded": 1, "files": 1, "ok": True})
    # The start-of-run stale sweep legitimately opens a client; a not-ok drain must still skip the
    # supersede DELETEs it gates.
    ch_deletes = []
    monkeypatch.setattr(bar, "ch_client", lambda: _FakeCHClient(ch_deletes))

    rc = bar.run(execute=True, sleep=0.0)
    assert rc == 1
    assert ch_deletes == []


def _landed_all_but(missing_hex):
    def _rd(_day, targets):
        landed = [h for h in targets if h != missing_hex]
        return {"fetched": len(targets), "landed": len(landed),
                "missing": len(targets) - len(landed), "errors": 0,
                "rows": len(landed), "path_rows": len(landed), "landed_hexes": sorted(landed)}
    return _rd


def test_execute_deletes_superseded_rows_after_loaders_landed_only(monkeypatch, capsys):
    # bbb222's trace is missing that day, so only aaa111 (landed) gets its old bronze rows deleted.
    _d, _f, _l, events, ch_deletes = _stub_flow(
        monkeypatch, [("aaa111", "2026-06-25"), ("bbb222", "2026-06-25")],
        run_daily_result=_landed_all_but("bbb222"), cleared=3)
    rc = bar.run(execute=True, sleep=0.0, accept_missing=True)  # accept so the missing hex alone keeps rc=0
    assert rc == 0
    # Deletes fire after both loaders, one per bronze table, landed hex only, run_start bound.
    assert events.index(("load", "path")) == len(events) - 1  # loads are the last events (deletes go via ch_client)
    assert [t for t, _p in ch_deletes] == [
        "bronze.adsblol_flight_segments", "bronze.adsblol_flight_paths"]
    params = [p for _t, p in ch_deletes]
    assert all(p["day"] == "2026-06-25" and p["hexes"] == ["aaa111"] for p in params)
    run_starts = {p["run_start"] for p in params}
    assert len(run_starts) == 1 and isinstance(run_starts.pop(), str)  # one stamp for the whole run
    assert "2026-06-25: cleared_old=6" in capsys.readouterr().out  # 3 + 3 across both tables


def test_drain_not_ok_skips_ch_deletes(monkeypatch):
    _d, _f, _l, _events, ch_deletes = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25")], drain_ok=False)
    rc = bar.run(execute=True, sleep=0.0)
    assert rc == 1
    assert ch_deletes == []  # replacement data didn't verifiably land -> never delete the old rows


def test_missing_traces_fail_run_unless_accepted(monkeypatch):
    pairs = [("aaa111", "2026-06-25"), ("bbb222", "2026-06-25")]
    _d, _f, _l, _e, _c = _stub_flow(monkeypatch, pairs, run_daily_result=_landed_all_but("bbb222"))
    assert bar.run(execute=True, sleep=0.0) == 1  # missing>0 fails by default
    _d, _f, _l, _e, _c = _stub_flow(monkeypatch, pairs, run_daily_result=_landed_all_but("bbb222"))
    assert bar.run(execute=True, sleep=0.0, accept_missing=True) == 0  # accepted -> clean exit


def test_errors_fail_run_even_with_accept_missing(monkeypatch):
    def _rd(_day, targets):
        return {"fetched": len(targets), "landed": 0, "missing": 0, "errors": len(targets),
                "rows": 0, "path_rows": 0, "landed_hexes": []}
    _d, _f, _l, _e, _c = _stub_flow(monkeypatch, [("a61c53", "2026-06-25")], run_daily_result=_rd)
    # --accept-missing only forgives missing traces, never fetch errors.
    assert bar.run(execute=True, sleep=0.0, accept_missing=True) == 1


def test_accept_missing_arg_defaults_false():
    assert bar._parse_args([]).accept_missing is False
    assert bar._parse_args(["--accept-missing"]).accept_missing is True


def test_stale_hexdays_sql_shape():
    sql = bar._STALE_HEXDAYS_SQL
    assert "{table}" in sql
    assert "FINAL" in sql
    assert "uniqExact(ingested_at) > 1" in sql
    assert "GROUP BY trace_day, icao24" in sql


def test_sweep_stale_dry_run_mutates_nothing(capsys):
    from datetime import date, datetime

    mx = datetime(2026, 7, 10, 2, 43, 0)
    fake = _FakeCHClient([], stale_rows=[(date(2026, 6, 4), "7800ff", mx),
                                          (date(2026, 6, 4), "abc123", mx)])
    bar.sweep_stale(client=fake, execute=False)
    assert fake._sink == []
    out = capsys.readouterr().out
    assert "2026-06-04" in out
    assert "2 hex-days" in out
    assert "dry-run" in out


def test_sweep_stale_execute_groups_by_day_and_batch(capsys):
    from datetime import date, datetime

    t1 = datetime(2026, 7, 10, 2, 43, 0)
    t2 = datetime(2026, 7, 10, 3, 15, 0)
    stale = [(date(2026, 6, 4), "7800ff", t1), (date(2026, 6, 4), "abc123", t1),
             (date(2026, 6, 4), "ffff01", t2)]
    ch_deletes = []
    fake = _FakeCHClient(ch_deletes, stale_rows=stale)

    bar.sweep_stale(client=fake, execute=True)

    assert len(ch_deletes) == 4  # 2 groups x 2 tables
    assert [t for t, _p in ch_deletes] == [
        "bronze.adsblol_flight_segments", "bronze.adsblol_flight_segments",
        "bronze.adsblol_flight_paths", "bronze.adsblol_flight_paths",
    ]
    t1_params = [p for _t, p in ch_deletes if p["mx"] == t1]
    assert len(t1_params) == 2
    assert all(p == {"day": "2026-06-04", "hexes": ["7800ff", "abc123"], "mx": t1} for p in t1_params)
    t2_params = [p for _t, p in ch_deletes if p["mx"] == t2]
    assert len(t2_params) == 2
    assert all(p["hexes"] == ["ffff01"] for p in t2_params)
    # Per-delete pre-count (lightweight DELETE reports written_rows=0), not a running total.
    assert "cleared_rows=3" in capsys.readouterr().out


def test_run_execute_sweeps_before_affected_pairs(monkeypatch):
    from datetime import date, datetime

    mx = datetime(2026, 7, 10, 2, 43, 0)
    _deleted, _fetched, _loaded, _events, ch_deletes = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25")],
        stale_rows=[(date(2026, 6, 4), "7800ff", mx)])

    stubbed_affected_pairs = bar.affected_pairs
    snapshot = {}

    def wrapped():
        snapshot["at_call"] = len(ch_deletes)
        return stubbed_affected_pairs()

    monkeypatch.setattr(bar, "affected_pairs", wrapped)

    rc = bar.run(execute=True, sleep=0.0)
    assert rc == 0
    # Both tables swept (1 stale hex-day each) before affected_pairs() runs.
    assert snapshot["at_call"] == 2
    assert "mx" in ch_deletes[0][1] and "mx" in ch_deletes[1][1]
    assert "run_start" in ch_deletes[2][1] and "run_start" in ch_deletes[3][1]


def test_run_execute_with_days_limit_keeps_sweep_report_only(monkeypatch, capsys):
    from datetime import date, datetime

    mx = datetime(2026, 7, 10, 2, 43, 0)
    _deleted, _fetched, _loaded, _events, ch_deletes = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25")],
        stale_rows=[(date(2026, 6, 4), "7800ff", mx)])

    rc = bar.run(execute=True, sleep=0.0, days_limit=1)
    assert rc == 0
    # A pilot run (--days) must never mutate beyond its window; only the post-drain, run_start-bound
    # deletes for the piloted day are allowed through.
    assert all("mx" not in p for _t, p in ch_deletes)
    assert "report-only under --days" in capsys.readouterr().out


def test_sweep_stale_flag_dispatch(monkeypatch):
    assert bar._parse_args([]).sweep_stale is False
    assert bar._parse_args(["--sweep-stale"]).sweep_stale is True

    calls = []
    monkeypatch.setattr(bar, "sweep_stale", lambda *, execute: calls.append(execute) or None)

    def _no_run(*_a, **_k):
        raise AssertionError("run() must not be called when --sweep-stale is set")
    monkeypatch.setattr(bar, "run", _no_run)

    assert bar.main(["--sweep-stale"]) == 0
    assert calls == [False]
    assert bar.main(["--sweep-stale", "--execute"]) == 0
    assert calls == [False, True]
