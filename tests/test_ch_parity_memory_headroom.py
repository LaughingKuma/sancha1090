import re
from pathlib import Path

import pytest

from include import ch_parity as p

_CANARY_ROWS = [(43000, 43000)]


def _fake(rows, *, canary=_CANARY_ROWS):
    # Dispatches on the SQL text: the canary query runs first (fail-closed gate),
    # then the main offender query -- a fixture must answer both distinctly.
    def q(sql):
        return canary if "SELECT count()" in sql else rows
    return q


def _raising_fake(exc):
    def q(_sql):
        raise exc
    return q


def test_memory_headroom_gate_passes_when_no_offenders():
    out = p.run_memory_headroom_gate(ch_query=_fake([]))
    assert out == {"all_ok": True, "tagged_rows": 43000}


def test_memory_headroom_gate_raises_on_ratio_breach():
    # 0.85: a row the SQL could actually emit (>= the 0.8 HAVING threshold) -- thresholding
    # itself lives in the SQL's HAVING clause, this only checks Python surfaces a returned row.
    rows = [("model.sancha1090.fct_flights_reconciled", 0.85, 10.2, 12.0, 0, 0)]
    with pytest.raises(RuntimeError, match="memory headroom gate FAILED") as exc:
        p.run_memory_headroom_gate(ch_query=_fake(rows))
    msg = str(exc.value)
    assert "model.sancha1090.fct_flights_reconciled" in msg
    assert "0.85" in msg


def test_memory_headroom_gate_raises_on_spill_only_row():
    # ratio well below _MEM_HEADROOM_RATIO, but spilled_runs > 0 must still red.
    rows = [("model.sancha1090.int_flight_attach", 0.10, 1.2, 12.0, 3, 0)]
    with pytest.raises(RuntimeError, match="memory headroom gate FAILED") as exc:
        p.run_memory_headroom_gate(ch_query=_fake(rows))
    msg = str(exc.value)
    assert "model.sancha1090.int_flight_attach" in msg
    assert "spilled_runs=3" in msg


def test_memory_headroom_gate_raises_on_oom_kill_only_row():
    # ratio 0.0 and no spills, but an admission-kill (code 241) has no QueryFinish row and
    # memory_usage=0 -- invisible to both other arms, must still red on oom_kills alone.
    rows = [("model.sancha1090.int_swim_latest", 0.0, 0.0, 12.0, 0, 1)]
    with pytest.raises(RuntimeError, match="memory headroom gate FAILED") as exc:
        p.run_memory_headroom_gate(ch_query=_fake(rows))
    msg = str(exc.value)
    assert "model.sancha1090.int_swim_latest" in msg
    assert "oom_kills=1" in msg


def test_memory_headroom_gate_raises_on_canary_zero():
    # Canary parses 0 nodes and the LIKE prefilter matches nothing: fail closed, name the no-run cause.
    with pytest.raises(RuntimeError, match="memory headroom gate FAILED") as exc:
        p.run_memory_headroom_gate(ch_query=_fake([], canary=[(0, 0)]))
    assert "no node-tagged dbt query" in str(exc.value)


def test_memory_headroom_gate_raises_on_canary_format_drift():
    # LIKE prefilter still matches but the exact extract regex parses nothing -- the failure mode a
    # bare row count could not see (e.g. two spaces after "node_id":); must red naming format drift.
    with pytest.raises(RuntimeError, match="memory headroom gate FAILED") as exc:
        p.run_memory_headroom_gate(ch_query=_fake([], canary=[(120, 0)]))
    assert "format drifted" in str(exc.value)


def test_memory_headroom_gate_fails_closed_on_query_error():
    with pytest.raises(RuntimeError, match="memory headroom gate FAILED") as exc:
        p.run_memory_headroom_gate(ch_query=_raising_fake(RuntimeError("boom: connection reset")))
    assert "boom: connection reset" in str(exc.value)


# --- SQL-text pins: guard a future max(memory)/any(cap) regression -------------------------------

def test_memory_headroom_sql_pins_spill_and_exception_signals():
    assert "ExternalAggregationWritePart" in p._MEM_HEADROOM_SQL
    assert "ExternalSortWritePart" in p._MEM_HEADROOM_SQL
    assert "ExternalJoinWritePart" in p._MEM_HEADROOM_SQL
    assert "ExceptionWhileProcessing" in p._MEM_HEADROOM_SQL
    assert "ExceptionBeforeStart" in p._MEM_HEADROOM_SQL
    assert "oom_kills" in p._MEM_HEADROOM_SQL


def test_memory_headroom_sql_threshold_matches_constant():
    # Thresholding lives in the SQL HAVING clause, not a Python-side string-pinned constant --
    # assert the ratio constant AND the HAVING arm text that actually applies it.
    assert p._MEM_HEADROOM_RATIO == 0.8
    assert "HAVING max_ratio >=" in p._MEM_HEADROOM_SQL


def test_memory_headroom_sql_shares_node_regex_with_canary():
    # The canary's parsed count must use the exact regex the main query filters by, or a
    # comment-format drift greens the main query while the canary stays fat (P2 review finding).
    assert p._MEM_HEADROOM_NODE_RE in p._MEM_HEADROOM_SQL
    assert p._MEM_HEADROOM_NODE_RE in p._MEM_HEADROOM_CANARY_SQL


def test_memory_headroom_node_regex_matches_representative_comment():
    # Behavior check of the escaping chain: the CH string literal unescapes \\ -> \, and THAT
    # regex must extract a real dbt node id -- pins the doubled backslashes as intentional.
    ch_unescaped = p._MEM_HEADROOM_NODE_RE.replace("\\\\", "\\")
    m = re.search(ch_unescaped, '/* {"node_id": "model.sancha1090.fct_flight_path"} */')
    assert m and m.group(1) == "model.sancha1090.fct_flight_path"
    assert not re.search(ch_unescaped, '/* {"node_id": "operation.sancha1090.x"} */')


def test_memory_headroom_sql_worst_row_is_one_argmax():
    # peak_gb and cap_gb must come from ONE argMax over a tuple -- two separate argMax calls can pick
    # different rows when ratios tie.
    assert p._MEM_HEADROOM_SQL.count("argMax(tuple(mem, cap), ratio)") == 2
    assert "argMax(mem," not in p._MEM_HEADROOM_SQL
    assert "argMax(cap," not in p._MEM_HEADROOM_SQL


def test_memory_headroom_sql_computes_ratio_per_row():
    # Caps change mid-window (fct_flights_reconciled 16e9->12e9 on 2026-08-24) -- a pooled
    # max(memory)/any(cap) form can pair one query's memory with another's cap and misreport.
    assert "memory_usage / cap AS ratio" in p._MEM_HEADROOM_SQL


def test_memory_headroom_sql_max_memory_fallback_pins_profile_default():
    # Pins the 16e9 fallback in _MEM_HEADROOM_SQL to the same value the CH profile actually
    # applies, so a resource_limits.xml change can't silently orphan the SQL-side fallback.
    xml_path = Path(__file__).resolve().parents[1] / "clickhouse" / "users.d" / "resource_limits.xml"
    xml = xml_path.read_text()
    m = re.search(r"<max_memory_usage>(\d+)</max_memory_usage>", xml)
    assert m, "max_memory_usage not found in resource_limits.xml"
    assert m.group(1) in p._MEM_HEADROOM_SQL
