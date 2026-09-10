from __future__ import annotations

import scripts.backfill_adsblol_resegment as bar
from include.adsblol_routes import (
    DWELL_GS_KT,
    DWELL_S,
    LOW_FIX_ALT_FT,
    SLOW_GAP_CEIL_FT,
    SLOW_GAP_S,
    SLOW_GAP_SPEED_KMH,
)


SLOW_GAP_SQL, DWELL_SQL, TAKEOFF_SQL = bar.affected_sqls()


def test_affected_sql_interpolates_task1_constants():
    sql = SLOW_GAP_SQL
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


def test_run_sql_template_mirrors_parse_trace_runs():
    tpl = bar._RUN_SQL
    # One run finder for both _parse_trace mirrors: {pred} opens a run, a SLOW_GAP_S silence ends it (the walk
    # drops the all-ground piece there, never persisted), the floor is DWELL_S on the persisted integer grid.
    assert "{pred} AS in_run" in tpl
    assert "lagInFrame(toNullable(in_run), 1, NULL)" in tpl
    assert f"OR toUnixTimestamp(ts) - toUnixTimestamp(prev_ts) >= {SLOW_GAP_S}" in tpl
    assert f"toUnixTimestamp(max(ts)) - toUnixTimestamp(min(ts)) >= {DWELL_S}\n     AND {{having}}" in tpl
    assert "WHERE in_run\n" in tpl
    # Whole-day windows spanning the old segment breaks (the walk runs before them); the table stays a slot
    # for the end-to-end fixture's scratch table.
    assert "PARTITION BY icao24, trace_day ORDER BY ts" in tpl
    assert "seg_start" not in tpl
    assert "AS prev_ts{extra_select}\n  FROM {table} FINAL\n  WHERE trace_day BETWEEN %(lo)s AND %(hi)s" in tpl
    assert "UNION" not in tpl


def test_dwell_sql_is_the_run_finder_over_dwell_fix():
    sql = DWELL_SQL
    # Same three guards as _dwell_fix, NULL failing open; only a run still holding an unflagged fix selects,
    # which is what makes a re-landed hex-day drop out of the next dry run.
    assert bar._DWELL_PRED == f"coalesce(alt_ft < {LOW_FIX_ALT_FT} AND gs_kt < {DWELL_GS_KT}, false)"
    assert f"{bar._DWELL_PRED} AS in_run" in sql
    assert "AND countIf(NOT gnd) > 0\n" in sql
    assert "1800" in sql and "984.0" in sql and "30.0" in sql
    assert "next_gnd" not in sql


def test_takeoff_sql_is_the_run_finder_over_the_ground_flag():
    sql = TAKEOFF_SQL
    # A ground run on the flag alone (flagged or dwell-derived, so no gs/alt guard) whose next persisted fix is
    # airborne (NULL at day end = no trim); the select-list alias is reused, ClickHouse resolves it.
    assert bar._TAKEOFF_PRED == "gnd"
    assert "coalesce(on_ground, false) AS gnd,\n    gnd AS in_run" in sql
    assert "leadInFrame(toNullable(gnd), 1, NULL)" in sql
    assert "ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING) AS next_gnd\n  FROM" in sql
    assert "AND argMax(tuple(next_gnd), ts).1 = false\n" in sql
    assert "gs_kt" not in sql and "alt_ft" not in sql


def test_affected_sqls_is_the_three_arms_formatted_over_the_table():
    # One wave selects every arm, as separate statements: a UNION sorts the paths table twice at once.
    assert bar.affected_sqls() == (SLOW_GAP_SQL, DWELL_SQL, TAKEOFF_SQL)
    assert len(bar.AFFECTED_SQL_TEMPLATES) == 3
    for arm in bar.affected_sqls():
        assert "UNION" not in arm and "{" not in arm
        assert "bronze.adsblol_flight_paths FINAL\n  WHERE trace_day BETWEEN %(lo)s AND %(hi)s" in arm
    for arm in bar.affected_sqls(table="bronze.scratch_paths"):
        assert "FROM bronze.scratch_paths FINAL" in arm and "adsblol_flight_paths" not in arm


class _SelectorClient:
    def __init__(self, rows_by_sql, span):
        self._rows = rows_by_sql
        self._span = span
        self.windows = []
        self.closed = False

    def query(self, sql, parameters=None, **_kw):
        if sql.startswith("SELECT minOrNull(trace_day), maxOrNull(trace_day) FROM "):
            self.span_sql = sql
            return _FakeQueryResult([self._span])
        self.windows.append((sql, parameters["lo"], parameters["hi"]))
        # Only the first week of each arm carries rows; later windows are empty like a quiet fortnight.
        return _FakeQueryResult(self._rows.get(sql, []) if parameters["lo"] == self._span[0].isoformat() else [])

    def close(self):
        self.closed = True


def test_affected_pairs_walks_weekly_windows_per_arm_lowercases_and_sorts_by_day_then_hex():
    from datetime import date

    client = _SelectorClient({
        SLOW_GAP_SQL: [("A61C53", date(2026, 6, 26)), ("ffff01", date(2026, 6, 25))],
        DWELL_SQL: [("a61c53", date(2026, 6, 26)), ("863b10", date(2026, 6, 25))],
        TAKEOFF_SQL: [("a7615f", date(2026, 6, 28)), ("863b10", date(2026, 6, 25))],
    }, span=(date(2026, 6, 25), date(2026, 7, 9)))
    assert bar.affected_pairs(client=client) == [
        ("863b10", "2026-06-25"), ("ffff01", "2026-06-25"), ("a61c53", "2026-06-26"), ("a7615f", "2026-06-28")]
    # 15 days of paths = three 7-day windows per arm, contiguous and non-overlapping, arms in order.
    weeks = [("2026-06-25", "2026-07-01"), ("2026-07-02", "2026-07-08"), ("2026-07-09", "2026-07-15")]
    assert client.windows == [(arm, lo, hi) for arm in bar.affected_sqls() for lo, hi in weeks]
    assert client.span_sql == "SELECT minOrNull(trace_day), maxOrNull(trace_day) FROM bronze.adsblol_flight_paths"
    assert client.closed is False  # a caller-supplied client stays open


def test_affected_pairs_on_empty_paths_table_runs_no_selector():
    # minOrNull/maxOrNull return NULL over no rows; plain min/max would return 1970-01-01 and walk from there.
    client = _SelectorClient({}, span=(None, None))
    assert bar.affected_pairs(client=client) == []
    assert client.windows == []


def test_affected_pairs_table_hook_rewrites_every_statement():
    from datetime import date

    client = _SelectorClient({}, span=(date(2026, 6, 25), date(2026, 6, 25)))
    assert bar.affected_pairs(client=client, table="bronze.scratch_paths") == []
    assert client.span_sql.endswith(" FROM bronze.scratch_paths")
    assert len(client.windows) == len(bar.affected_sqls())
    for sql, _lo, _hi in client.windows:
        assert "FROM bronze.scratch_paths FINAL" in sql and "adsblol_flight_paths" not in sql


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


def _stub_flow(monkeypatch, pairs, *, drain_ok=True, seg_ok=None, path_ok=None,
               land_result=None, land_fn=None, cleared=3, stale_rows=None):
    monkeypatch.setattr(bar, "affected_pairs", lambda: pairs)
    events = []
    deleted = []

    def fake_delete(ps, *_a, **_k):
        deleted.append(list(ps))
        events.append(("delete", tuple(ps)))
        return len(ps)

    monkeypatch.setattr(bar.ledger, "delete_attempts", fake_delete)
    fetched = []

    def fake_land(day, targets=None, **_kw):
        fetched.append((day.isoformat(), tuple(sorted(targets))))
        events.append(("fetch", day.isoformat(), tuple(sorted(targets))))
        if land_result is not None:
            return land_result(day, sorted(targets))
        return {"fetched": len(targets) * 2, "landed": len(targets),
                "missing": 0, "errors": 0, "rows": len(targets), "path_rows": len(targets),
                "landed_hexes": sorted(targets)}

    monkeypatch.setattr(bar, "land_release_day", land_fn or fake_land)
    loaded = []

    def fake_load(name, ok):
        def _load(*_a, **_k):
            loaded.append(name)
            events.append(("load", name))
            return {"ch_loaded": 0, "files": 0, "ok": ok}
        return _load

    monkeypatch.setattr(bar, "load_adsblol_segments_pending_to_ch",
                        fake_load("seg", drain_ok if seg_ok is None else seg_ok))
    monkeypatch.setattr(bar, "load_adsblol_paths_pending_to_ch",
                        fake_load("path", drain_ok if path_ok is None else path_ok))
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


def test_execute_clears_ledger_then_reextracts_then_loads(monkeypatch):
    # A restartable script must clear-then-re-extract per day (never batch all deletes up front),
    # so a crash mid-run leaves at most one day's ledger rows in a gap.
    _deleted, _fetched, _loaded, events, ch_deletes = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25"), ("ffff01", "2026-06-25"),
                      ("a61c53", "2026-06-26")])
    rc = bar.run(execute=True)
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
    def fake_land(_day, _targets=None, progress=None, **_kw):
        if progress is not None:
            progress(20_000)
        return {"fetched": 2, "landed": 1, "missing": 0, "errors": 0, "rows": 1, "path_rows": 1,
                "landed_hexes": ["a61c53"]}

    _stub_flow(monkeypatch, [("a61c53", "2026-06-25")], land_fn=fake_land)
    bar.run(execute=True)
    assert "2026-06-25: 20000 members scanned" in capsys.readouterr().out


def test_days_limit_pilots_first_day_only(monkeypatch):
    _deleted, fetched, _loaded, _events, _ch = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25"), ("ffff01", "2026-06-26")])
    bar.run(execute=True, days_limit=1)
    assert fetched == [("2026-06-25", ("a61c53",))]


def test_execute_returns_nonzero_on_extract_errors_but_still_drains_loaders(monkeypatch):
    _d, _f, loaded, _e, _c = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25")],
        land_result=lambda _day, _targets: {
            "fetched": 2, "landed": 0, "missing": 0, "errors": 2, "rows": 0, "path_rows": 0,
            "landed_hexes": []})

    rc = bar.run(execute=True)
    assert rc == 1
    # The exit code signals re-run needed, but whatever landed must still drain to CH.
    assert loaded == ["seg", "path"]


def test_one_day_raising_still_runs_the_rest_and_reds_the_run(monkeypatch, capsys):
    seen: list[str] = []

    def fake_land(day, targets=None, **_kw):
        seen.append(day.isoformat())
        if day.isoformat() == "2026-06-25":
            raise RuntimeError("day failed quality gate")
        return {"fetched": 1, "landed": 1, "missing": 0, "errors": 0, "rows": 1, "path_rows": 1,
                "landed_hexes": sorted(targets)}

    _d, _f, loaded, _e, ch_deletes = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25"), ("ffff01", "2026-06-26")], land_fn=fake_land)
    rc = bar.run(execute=True)
    assert rc == 1
    assert seen == ["2026-06-25", "2026-06-26"]
    assert loaded == ["seg", "path"]
    # The raising day never landed a replacement, so its old bronze rows must survive.
    assert ch_deletes and all(p["day"] == "2026-06-26" for _t, p in ch_deletes)
    out = capsys.readouterr().out
    assert "2026-06-25: FAILED — day failed quality gate" in out


def test_execute_returns_nonzero_when_drain_reports_not_ok(monkeypatch):
    # The loaders are best-effort and never raise (include/clickhouse.py's _safe guard), so a
    # failed drain only surfaces via ok=False — run() must fold that into the exit status too.
    # The start-of-run stale sweep legitimately opens a client; a not-ok drain must still skip the
    # supersede DELETEs it gates.
    _d, _f, _l, _e, ch_deletes = _stub_flow(
        monkeypatch, [("a61c53", "2026-06-25")], seg_ok=False, path_ok=True)

    rc = bar.run(execute=True)
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
        land_result=_landed_all_but("bbb222"), cleared=3)
    rc = bar.run(execute=True, accept_missing=True)  # accept so the missing hex alone keeps rc=0
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
    rc = bar.run(execute=True)
    assert rc == 1
    assert ch_deletes == []  # replacement data didn't verifiably land -> never delete the old rows


def test_missing_traces_fail_run_unless_accepted(monkeypatch):
    pairs = [("aaa111", "2026-06-25"), ("bbb222", "2026-06-25")]
    _d, _f, _l, _e, _c = _stub_flow(monkeypatch, pairs, land_result=_landed_all_but("bbb222"))
    assert bar.run(execute=True) == 1  # missing>0 fails by default
    _d, _f, _l, _e, _c = _stub_flow(monkeypatch, pairs, land_result=_landed_all_but("bbb222"))
    assert bar.run(execute=True, accept_missing=True) == 0  # accepted -> clean exit


def test_errors_fail_run_even_with_accept_missing(monkeypatch):
    def _rd(_day, targets):
        return {"fetched": len(targets), "landed": 0, "missing": 0, "errors": len(targets),
                "rows": 0, "path_rows": 0, "landed_hexes": []}
    _d, _f, _l, _e, _c = _stub_flow(monkeypatch, [("a61c53", "2026-06-25")], land_result=_rd)
    # --accept-missing only forgives missing traces, never extraction errors.
    assert bar.run(execute=True, accept_missing=True) == 1


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

    rc = bar.run(execute=True)
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

    rc = bar.run(execute=True, days_limit=1)
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
