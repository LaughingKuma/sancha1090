from __future__ import annotations

from collections import namedtuple
from datetime import date

import include.adsblol_route_ledger as ledger
import scripts.backfill_adsblol_class1 as bc
from conftest import RecordingCH


def test_candidates_sql_shape():
    sql = bc.CANDIDATES_SQL
    # Only dest-NULL flights are class-1 candidates; the origin side is a different defect.
    assert "dest_icao IS NULL" in sql
    assert "fct_flights_reconciled" in sql
    # FINAL: the RMT keeps superseded segment rows until merge, and a stale one lies about the cut.
    assert "adsblol_flight_segments FINAL" in sql
    assert "lower(f.icao24)" in sql
    assert "INTERVAL 23 HOUR + INTERVAL 50 MINUTE" in sql
    assert "last_gnd = 0" in sql
    assert "SELECT DISTINCT hex, trace_day" in sql
    # INNER join: a flight with no overlapping segment must never become a candidate.
    assert "left join" not in sql.lower()
    # Partition prune: nothing past the cutover can be class-1.
    assert "trace_day <= toDate('{cutover_day}')" in sql
    assert "{gold}" in sql  # schema resolved at call time, matching include/adsblol_routes.py


def test_live_era_landed_sql_shape():
    sql = ledger.LIVE_ERA_LANDED_SQL
    assert "adsblol_route_attempts" in sql
    assert "outcome = 'landed'" in sql
    # Full predicates, direction included: lag >= 2 fetches got the final trace, and everything
    # stamped past the cutover is a tar landing, never a truncation.
    assert "attempted_at::date - trace_day::date <= 1 " in sql
    assert "attempted_at < :cutover" in sql
    # A failed recovery attempt re-stamps the pair post-cutover as 'error'; without this arm it
    # would vanish from every later run and the dry run would falsely read clean.
    assert "OR outcome = 'error'" in sql
    # Bounded pull: the backlog ledger holds ~200k landed rows, the candidates a few hundred.
    assert "trace_day IN :days" in sql
    assert "icao24 IN :hexes" in sql


_Row = namedtuple("_Row", "icao24 trace_day")


def test_landed_live_era_bounds_params_and_filters_to_given_pairs(monkeypatch):
    seen: dict = {}

    class _Conn:
        def execute(self, _stmt, params):
            seen.update(params)
            # ffff01/06-26 is landed in the store but NOT a candidate pair -> must not leak through.
            return [_Row("a61c53", "2026-06-25"), _Row("ffff01", "2026-06-26")]

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Eng:
        def begin(self):
            return _Conn()

    monkeypatch.setattr(ledger, "_prepare", lambda _e: _Eng())
    pairs = [("a61c53", "2026-06-25"), ("a61c53", "2026-06-26"), ("ffff02", "2026-06-25")]
    assert ledger.landed_live_era(pairs, "CUTOVER") == [("a61c53", "2026-06-25")]
    assert seen == {"days": ["2026-06-25", "2026-06-26"],
                    "hexes": ["a61c53", "ffff02"], "cutover": "CUTOVER"}


def test_landed_live_era_empty_pairs_short_circuits(monkeypatch):
    def _boom(_e):
        raise AssertionError("no engine work for empty pairs")
    monkeypatch.setattr(ledger, "_prepare", _boom)
    assert ledger.landed_live_era([], "CUTOVER") == []


def test_affected_pairs_intersects_and_sorts_by_day_then_hex(monkeypatch):
    ch = RecordingCH(responses={"fct_flights_reconciled": [
        ("ffff01", date(2026, 6, 26)), ("a61c53", date(2026, 6, 26)), ("a61c53", date(2026, 6, 25))]})
    calls: list = []

    def fake_live_era(pairs, cutover, engine=None):
        calls.append((pairs, cutover, engine))
        return [p for p in pairs if p[0] != "ffff01"]  # ffff01's cut is a coverage hole, not live-era

    monkeypatch.setattr(bc, "ch_client", lambda: ch)
    monkeypatch.setattr(bc.ledger, "landed_live_era", fake_live_era)

    assert bc.affected_pairs() == [("a61c53", "2026-06-25"), ("a61c53", "2026-06-26")]
    assert ch.closed
    # CH Dates reach the ledger as ISO strings (its trace_day is TEXT), cutover pinned.
    assert calls == [([("ffff01", "2026-06-26"), ("a61c53", "2026-06-26"), ("a61c53", "2026-06-25")],
                      bc.TAR_CUTOVER, None)]
    assert "toDate('2026-08-30')" in ch.queries[0]  # cutover day interpolated into the prune


def test_main_delegates_to_wave_cli_with_this_selector(monkeypatch):
    calls = []
    # Sentinel exit code: main() must surface run()'s status, the operator's only failure signal.
    monkeypatch.setattr(bc.resegment, "run", lambda **kw: calls.append(kw) or 7)
    assert bc.main(["--execute", "--days", "2", "--accept-missing"]) == 7
    assert calls == [{"execute": True, "days_limit": 2, "accept_missing": True,
                      "affected": bc.affected_pairs}]


def test_dry_run_is_the_default(monkeypatch):
    calls = []
    monkeypatch.setattr(bc.resegment, "run", lambda **kw: calls.append(kw) or 0)
    assert bc.main([]) == 0
    assert calls == [{"execute": False, "days_limit": None, "accept_missing": False,
                      "affected": bc.affected_pairs}]
