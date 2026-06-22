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
          "bronze.opensky_states": 100, "bronze.archive_states": 100}
_PARQUET = {"flights_raw": 100, "adsb_state": 100, "states,states_raw": 100, "archive_states_raw": 100}
_NOW = 1_780_000_000
_FRESH_OK = {"agg_hourly_traffic": _NOW - 60, "fact_state_snapshots": _NOW - 60,
             "agg_country_traffic": _NOW - 60, "fct_adsb_state": _NOW - 60, "fact_flights": _NOW - 60}


def _completeness_fake(ch_counts, parquet_counts):
    # one fn drives both sides of each completeness check: CH bronze count vs s3() Parquet count.
    def q(sql):
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
    assert out["passed"] == out["total"] == 9   # 4 completeness + 5 freshness (states/context + adsb + flights; anomalies excluded)


def test_source_gate_raises_when_ch_short_of_source():
    ch_short = dict(_CH_OK, **{"bronze.opensky_flights": 80})   # 20% short = data loss
    with pytest.raises(RuntimeError, match="source gate FAILED"):
        p.run_source_gate(
            ch_query=_completeness_fake(ch_short, _PARQUET),
            serving_query=_freshness_fake(_NOW, _FRESH_OK))


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
