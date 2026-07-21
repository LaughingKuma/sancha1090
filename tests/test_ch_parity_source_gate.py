import pytest

from include import ch_parity as p


def test_complete_comparator():
    cmp = p.complete(0.02)
    assert cmp(100, 100)        # exact match
    assert cmp(110, 100)        # surplus (e.g. the opensky_states P2 dup) passes
    assert cmp(99, 100)         # 1% trail (one in-flight ingest tick) tolerated
    assert not cmp(97, 100)     # 3% short = real load loss (the Iceberg failure mode)
    assert cmp(5, 0)            # empty source never fails the gate


def test_exact_comparator():
    cmp = p.exact()
    assert cmp(100, 100)        # equality
    assert not cmp(110, 100)    # surplus is NOT tolerated — it could offset a missing row
    assert not cmp(99, 100)     # short
    assert cmp(0, 0)            # empty source + empty CH


# --- run_source_gate with injected queriers (no live CH / Trino) -----------------------------------
# The gate is EXACT, so the OK fixture must match the source row-for-row (no surplus).
_CH_OK = {"bronze.opensky_flights": 100, "bronze.adsb_states": 100,
          "bronze.opensky_states": 100, "bronze.adsblol_states": 100}
_PARQUET = {"flights_raw": 100, "adsb_state": 100, "states,states_raw": 100, "adsblol_states_raw": 100}
_NOW = 1_780_000_000
_FRESH_OK = {"agg_hourly_traffic": _NOW - 60, "fact_state_snapshots": _NOW - 60,
             "agg_country_traffic": _NOW - 60, "fct_adsb_state": _NOW - 60, "fact_flights": _NOW - 60,
             "fct_flights_reconciled": _NOW - 60}


def _completeness_fake(ch_counts, parquet_counts, broken_parts=()):
    # one fn drives both sides of each completeness check: CH bronze count vs s3() Parquet count. Also answers
    # the broken-parts tripwire (same admin client) so every completeness-fake caller stays green by default.
    def q(sql):
        if "detached_parts" in sql:
            return [[p] for p in broken_parts]
        src = parquet_counts if "s3(" in sql else ch_counts
        for token, val in src.items():
            if token in sql:
                return [[val]]
        raise AssertionError(f"unmapped completeness sql: {sql}")
    return q


def _freshness_fake(now_epoch, mart_max):
    def q(sql):
        if "now()" in sql:
            return [[now_epoch]]
        for token, val in mart_max.items():
            if token in sql:
                return [[val]]
        raise AssertionError(f"unmapped freshness sql: {sql}")
    return q


def test_source_gate_passes_when_complete_and_fresh():
    out = p.run_source_gate(
        ch_query=_completeness_fake(_CH_OK, _PARQUET),
        serving_query=_freshness_fake(_NOW, _FRESH_OK))
    assert out["all_ok"]
    # 4 completeness + 6 freshness (states/context + adsb + flights + reconciled; anomalies excluded) + 1 tripwire
    assert out["passed"] == out["total"] == 11


def test_source_gate_raises_when_ch_short_of_source():
    ch_short = dict(_CH_OK, **{"bronze.opensky_flights": 80})   # 20% short = data loss
    with pytest.raises(RuntimeError, match="source gate FAILED"):
        p.run_source_gate(
            ch_query=_completeness_fake(ch_short, _PARQUET),
            serving_query=_freshness_fake(_NOW, _FRESH_OK))


# --- #116: broken-on-start detached-parts tripwire ------------------------------------------------

def test_source_gate_passes_when_no_broken_parts():
    out = p.run_source_gate(
        ch_query=_completeness_fake(_CH_OK, _PARQUET),
        serving_query=_freshness_fake(_NOW, _FRESH_OK))
    by_name = {r["check"]: r for r in out["results"]}
    assert by_name["source.no_broken_parts"]["ok"]


def test_source_gate_raises_and_names_the_broken_part():
    broken = _completeness_fake(_CH_OK, _PARQUET,
                                 broken_parts=["gold_ch.agg_hourly_traffic_acc/broken-on-start_all_1_1_0"])
    with pytest.raises(RuntimeError, match="source gate FAILED") as exc:
        p.run_source_gate(ch_query=broken, serving_query=_freshness_fake(_NOW, _FRESH_OK))
    msg = str(exc.value)
    assert "no_broken_parts" in msg
    assert "gold_ch.agg_hourly_traffic_acc/broken-on-start_all_1_1_0" in msg


def test_source_gate_raises_when_tripwire_query_errors():
    # A tripwire QUERY failure (not a real broken part) must still be diagnosable, not an anonymous red.
    base = _completeness_fake(_CH_OK, _PARQUET)

    def flaky(sql):
        if "detached_parts" in sql:
            raise RuntimeError("boom: connection reset")
        return base(sql)

    with pytest.raises(RuntimeError, match="source gate FAILED") as exc:
        p.run_source_gate(ch_query=flaky, serving_query=_freshness_fake(_NOW, _FRESH_OK))
    msg = str(exc.value)
    assert "no_broken_parts" in msg
    assert "boom: connection reset" in msg


def test_source_gate_raises_when_mart_stale():
    stale = dict(_FRESH_OK, **{"agg_hourly_traffic": _NOW - 99999})   # > 2h lag vs now()
    with pytest.raises(RuntimeError, match="source gate FAILED"):
        p.run_source_gate(
            ch_query=_completeness_fake(_CH_OK, _PARQUET),
            serving_query=_freshness_fake(_NOW, stale))


# --- P8a: the completeness gate is EXACT (no eps) over a closed window ----------------------------

def test_source_gate_is_exact_one_row_short_raises():
    # No eps: even one row short must red the gate — a relative tolerance would hide a missing file
    # (~hundreds of rows, far under any % of a 23M table).
    one_short = dict(_CH_OK, **{"bronze.opensky_states": 999})   # ref is 1000 -> exactly 1 short
    with pytest.raises(RuntimeError, match="source gate FAILED"):
        p.run_source_gate(
            ch_query=_completeness_fake(dict(one_short), {**_PARQUET, "states,states_raw": 1000}),
            serving_query=_freshness_fake(_NOW, _FRESH_OK))


def test_source_checks_use_one_cutoff_on_both_sides_and_content_metric():
    # The single captured cutoff must appear on BOTH sides of every windowed lane (no hour-boundary race), and
    # states must compare distinct CONTENT (the fingerprint) — a (icao24,snapshot_time) grain would hide a lost
    # recapture (23.04M distinct content vs 22.67M grains).
    cut = 1_782_100_000
    by_name = {name: (ch, ref) for name, ch, ref, _ in p.source_checks(cut)}
    for name in ("bronze.opensky_states.content_fp", "bronze.adsb_states.closed_grain", "bronze.opensky_flights.closed"):
        ch_sql, ref_sql = by_name[name]
        assert str(cut) in ch_sql and str(cut) in ref_sql, f"{name}: the one captured cutoff must be on both sides"
    states_ch, states_ref = by_name["bronze.opensky_states.content_fp"]
    fp = "cityHash64(toString(tuple("
    assert fp in states_ch and fp in states_ref, "states must compare distinct CONTENT, not (icao24,snapshot_time) grain"


def test_source_checks_pin_explicit_s3_structure():
    # 636-hardening: every source-side s3() read MUST pass explicit structure= so an empty/fresh-deploy glob
    # returns 0 rows instead of CANNOT_EXTRACT_TABLE_STRUCTURE (the gate runs */15 from first boot). Guards a
    # regression that silently drops structure= back to schema inference.
    for name, _ch_sql, ref_sql, _cmp in p.source_checks(1_782_100_000):
        if "s3(" in ref_sql:
            assert "structure=" in ref_sql, f"{name}: source s3() read missing explicit structure= (636 risk)"


# --- rung 1: fct_flight_path coverage gate --------------------------------------------------------

def _path_fake(now_epoch, path_head_epoch, share_rows):
    def q(sql):
        if "now()" in sql:
            return [[now_epoch]]
        if "n_adsblol" in sql:
            return share_rows
        if "max(day_key)" in sql:
            return [[path_head_epoch]]
        raise AssertionError(f"unmapped path-coverage sql: {sql}")
    return q


_PATH_ROWS_OK = [["2026-07-12", 5632, 0.955], ["2026-07-13", 5896, 0.943],
                 ["2026-07-14", 5762, 0.951]]


def test_path_coverage_gate_green_when_fresh_and_covered():
    out = p.run_path_coverage_gate(ch_query=_path_fake(_NOW, _NOW - 4 * 86400, _PATH_ROWS_OK))
    assert out["all_ok"]
    assert out["passed"] == out["total"] == 2


def test_path_coverage_gate_reds_on_stalled_mart():
    # 7 days > the 4.5-day tolerance (lag-1 healthy worst case 3d3h, + one tolerated 24h publish slip).
    with pytest.raises(RuntimeError, match="path coverage gate FAILED"):
        p.run_path_coverage_gate(ch_query=_path_fake(_NOW, _NOW - 7 * 86400, _PATH_ROWS_OK))


def test_path_coverage_freshness_exact_boundary():
    # fresh()'s comparator is (ref - ch) <= max_lag_s, so exactly at the tolerance still passes;
    # one second further stale trips it. Healthy share rows so only freshness varies.
    assert p._PATH_FRESHNESS_LAG_TOL_S == int(4.5 * 86400)
    tol = p._PATH_FRESHNESS_LAG_TOL_S
    out = p.run_path_coverage_gate(ch_query=_path_fake(_NOW, _NOW - tol, _PATH_ROWS_OK))
    assert out["all_ok"]
    with pytest.raises(RuntimeError, match="path coverage gate FAILED"):
        p.run_path_coverage_gate(ch_query=_path_fake(_NOW, _NOW - tol - 1, _PATH_ROWS_OK))


def test_path_coverage_gate_reds_on_share_cliff_and_names_the_day():
    rows = [["2026-07-12", 5632, 0.955], ["2026-07-13", 5896, 0.943], ["2026-07-14", 5762, 0.607]]
    with pytest.raises(RuntimeError, match="2026-07-14"):
        p.run_path_coverage_gate(ch_query=_path_fake(_NOW, _NOW - 4 * 86400, rows))


def test_path_coverage_gate_reds_on_empty_share_window():
    # A zero-row share window (e.g. spine truncation) must fail closed, not false-green.
    with pytest.raises(RuntimeError, match="share window empty"):
        p.run_path_coverage_gate(ch_query=_path_fake(_NOW, _NOW - 4 * 86400, []))


def test_path_coverage_gate_reds_on_short_window():
    # Round-3 operator decision: a short window (< _PATH_SHARE_DAYS) can conceal partial/truncated
    # reconciled data, so it now fails closed instead of passing-but-reported.
    rows = [["2026-07-13", 5896, 0.943], ["2026-07-14", 5762, 0.951]]
    with pytest.raises(RuntimeError, match="share window short"):
        p.run_path_coverage_gate(ch_query=_path_fake(_NOW, _NOW - 4 * 86400, rows))


def test_path_coverage_gate_reds_when_all_days_below_min_flights():
    rows = [["2026-07-16", 120, 0.10], ["2026-07-17", 200, 0.20], ["2026-07-18", 90, 0.05]]
    with pytest.raises(RuntimeError, match="no evaluable days"):
        p.run_path_coverage_gate(ch_query=_path_fake(_NOW, _NOW - 4 * 86400, rows))


def test_path_coverage_low_sample_day_skips_floor_but_is_reported():
    # A partial/low-flight day must not red the floor, but must surface as skipped — never silent.
    rows = [["2026-07-16", 5900, 0.95], ["2026-07-17", 5800, 0.94], ["2026-07-18", 120, 0.10]]
    out = p.run_path_coverage_gate(ch_query=_path_fake(_NOW, _NOW - 4 * 86400, rows))
    assert out["all_ok"]
    share = out["results"][-1]
    assert share["skipped"] == [("2026-07-18", 120, 0.10)]


def test_path_coverage_share_query_contract():
    # Pin the alarm contract: spine denominator via LEFT JOIN, spine-derived expected days
    # (a dropped interior partition must read as 0%, not yield its slot to an older day).
    assert p._PATH_ADSBLOL_SHARE_FLOOR == 0.75
    assert p._PATH_SHARE_MIN_FLIGHTS == 500
    assert p._PATH_SHARE_DAYS == 3
    assert "LEFT JOIN" in p._PATH_SHARE_SQL and "fct_flights_reconciled" in p._PATH_SHARE_SQL
    assert "max(day_key)" in p._PATH_SHARE_SQL
