import re

import pytest
import sqlalchemy as sa

from include import ch_served_value as v

H = 3600
_NOW = 1_780_000_000
_CUTOFF = _NOW // H * H - v._CLOSED_WINDOW_S
_LAST_CLOSED = _CUTOFF - H


# Drives the gate with no live CH: each query is routed by its table token (mirrors test_ch_parity_source_gate).
# adsb dicts: country keyed by (reg_country, hour) -> (distinct, obs, military); airline keyed by (name, country,
# hour) -> (distinct, obs, backfilled). Default empty so existing tests exercise only the opensky checks.
# The adsb branches are matched FIRST + assert distinctive SQL fragments: the airline oracle JOINs
# bronze.opensky_states for the callsign backfill, so it must route by its specific signature (dim_airlines +
# adsb_states) before the generic opensky-oracle branch, and the asserts catch a gate-SQL-shape regression in CI.
def _ch_fake(oracle, agg, fss, cc_oracle=None, cc_mart=None, aa_oracle=None, aa_mart=None):
    cc_oracle, cc_mart = cc_oracle or {}, cc_mart or {}
    aa_oracle, aa_mart = aa_oracle or {}, aa_mart or {}

    def q(sql):
        if "gold_ch.agg_country_traffic_adsb_acc" in sql:
            assert "uniqExactMerge(military_observations)" in sql and "snapshot_hour" in sql
            return [[g, h, ac, obs, mil] for (g, h), (ac, obs, mil) in cc_mart.items()]
        if "gold_ch.agg_airline_traffic_adsb_acc" in sql:
            assert "uniqExactMerge(backfilled_observations)" in sql and "snapshot_hour" in sql
            return [[nm, ct, h, ac, obs, bf] for (nm, ct, h), (ac, obs, bf) in aa_mart.items()]
        if "bronze.adsb_states" in sql and "dim_airlines" in sql:   # airline full-attribution oracle
            assert "argMinIf" in sql and "callsign_source = 'opensky_backfill'" in sql
            return [[nm, ct, h, ac, obs, bf] for (nm, ct, h), (ac, obs, bf) in aa_oracle.items()]
        if "bronze.adsb_states" in sql:                              # country oracle
            assert "toStartOfHour(toDateTime(capture_ts))" in sql and "bitAnd(db_flags, 1)" in sql
            return [[g, h, ac, obs, mil] for (g, h), (ac, obs, mil) in cc_oracle.items()]
        if "bronze.opensky_states" in sql:
            return [[h, obs, ac] for h, (obs, ac) in oracle.items()]
        if "gold_ch.agg_hourly_traffic" in sql:
            return [[h, obs, ac] for h, (obs, ac) in agg.items()]
        if "silver_ch.fact_state_snapshots" in sql:
            return [[h, obs] for h, obs in fss.items()]
        raise AssertionError(f"unmapped value sql: {sql}")
    return q


class _WM:
    # In-memory advance-only watermark stand-in for the postgres store.
    def __init__(self, start=None):
        self.value = start
        self.sets = []

    def get(self):
        return self.value

    def set(self, val):
        self.sets.append(val)
        if self.value is None or val > self.value:
            self.value = val


def _run(oracle, agg, fss, wm, **adsb):
    return v.run_value_gate(ch_query=_ch_fake(oracle, agg, fss, **adsb),
                            get_wm=wm.get, set_wm=wm.set, now_epoch=_NOW)


def test_value_gate_passes_and_advances_watermark():
    oracle = {_LAST_CLOSED: (10, 5), _LAST_CLOSED - H: (8, 4)}
    wm = _WM(start=None)
    out = _run(oracle, dict(oracle), {_LAST_CLOSED: 10, _LAST_CLOSED - H: 8}, wm)
    assert out["all_ok"]
    # agg_hourly + fss + agg_country_traffic_adsb + agg_airline_traffic_adsb (the adsb pair is empty here -> ok).
    assert out["passed"] == out["total"] == 4
    assert wm.value == _LAST_CLOSED                     # advanced to the newest fully-closed hour


def test_value_gate_reds_when_mv_drops_a_closed_hour():
    # The accumulate-forever failure mode: the oracle has the hour, the MV-backed serving view does not.
    oracle = {_LAST_CLOSED: (10, 5), _LAST_CLOSED - H: (8, 4)}
    agg = {_LAST_CLOSED - H: (8, 4)}                    # missing _LAST_CLOSED entirely
    wm = _WM(start=None)
    with pytest.raises(RuntimeError, match="served-value gate FAILED"):
        _run(oracle, agg, {_LAST_CLOSED: 10, _LAST_CLOSED - H: 8}, wm)
    assert wm.sets == []                                # watermark NOT advanced on failure


def test_value_gate_reds_on_count_mismatch():
    oracle = {_LAST_CLOSED: (10, 5)}
    agg = {_LAST_CLOSED: (9, 5)}                        # one observation short of bronze
    wm = _WM(start=None)
    with pytest.raises(RuntimeError, match="served-value gate FAILED"):
        _run(oracle, agg, {_LAST_CLOSED: 10}, wm)


def test_value_gate_reds_on_fss_mismatch_only():
    oracle = {_LAST_CLOSED: (10, 5)}
    wm = _WM(start=None)
    with pytest.raises(RuntimeError, match="served-value gate FAILED"):
        _run(oracle, dict(oracle), {_LAST_CLOSED: 7}, wm)   # fss short, agg fine


def test_lateness_recheck_catches_a_hour_below_the_watermark():
    # Watermark already at last_closed; a hour two below it goes bad -> the recheck window must still red it.
    bad_hour = _LAST_CLOSED - 2 * H
    oracle = {_LAST_CLOSED: (10, 5), bad_hour: (12, 6)}
    agg = {_LAST_CLOSED: (10, 5), bad_hour: (3, 6)}          # corrupted below the watermark
    wm = _WM(start=_LAST_CLOSED)
    with pytest.raises(RuntimeError, match="served-value gate FAILED"):
        _run(oracle, agg, {_LAST_CLOSED: 10, bad_hour: 12}, wm)


def test_phantom_mart_hour_with_no_source_reds():
    # A hour present in the mart but absent (zero) in the oracle is also a defect, not just a drop.
    oracle = {_LAST_CLOSED: (10, 5)}
    agg = {_LAST_CLOSED: (10, 5), _LAST_CLOSED - H: (4, 2)}  # phantom hour the source never had
    wm = _WM(start=None)
    with pytest.raises(RuntimeError, match="served-value gate FAILED"):
        _run(oracle, agg, {_LAST_CLOSED: 10}, wm)


def test_unbounded_catchup_validates_a_hour_far_below_the_watermark():
    # A long completeness outage froze the watermark ~10 days back; on recovery every held hour must be validated
    # (the retired 7-day backscan clamp would have skipped this one and lost the discrepancy forever).
    wm_start = _LAST_CLOSED - 10 * 24 * H
    bad = wm_start + H
    oracle = {_LAST_CLOSED: (10, 5), bad: (12, 6)}
    agg = {_LAST_CLOSED: (10, 5), bad: (3, 6)}              # corrupted held hour ~10 days back
    wm = _WM(start=wm_start)
    with pytest.raises(RuntimeError, match="served-value gate FAILED"):
        _run(oracle, agg, {_LAST_CLOSED: 10, bad: 12}, wm)


def test_fss_reds_on_aged_out_hours():
    # No retention floor: staging is full-history (2026-07 unwindowing), so fss missing an old
    # oracle-present hour is a real defect, not aging.
    wm_start = _LAST_CLOSED - 35 * 24 * H
    aged = wm_start + H
    oracle = {_LAST_CLOSED: (10, 5), aged: (7, 4)}
    with pytest.raises(RuntimeError, match="served-value gate FAILED"):
        _run(oracle, dict(oracle), {_LAST_CLOSED: 10}, _WM(start=wm_start))


def test_fss_query_bound_matches_oracle_bound():
    # Deep catch-up: pre-fix the fss query clamped its lower bound 29d above the oracle's — they must match now.
    wm_start = _LAST_CLOSED - 35 * 24 * H
    aged = wm_start + H
    oracle = {_LAST_CLOSED: (10, 5), aged: (7, 4)}
    inner = _ch_fake(oracle, dict(oracle), {_LAST_CLOSED: 10, aged: 7})
    captured = []

    def spy(sql):
        captured.append(sql)
        return inner(sql)
    wm = _WM(start=wm_start)
    v.run_value_gate(ch_query=spy, get_wm=wm.get, set_wm=wm.set, now_epoch=_NOW)
    bounds = {}
    for q in captured:
        m = re.search(r"FROM (\S+)\s.*?snapshot_time >= (.*?) AND snapshot_time <", q, re.DOTALL)
        if m:
            bounds[m.group(1)] = m.group(2)
    assert bounds["silver_ch.fact_state_snapshots"] == bounds["bronze.opensky_states"]


def test_agg_validated_on_deep_catchup():
    # agg discrepancy on a deep catch-up hour reds regardless of depth.
    wm_start = _LAST_CLOSED - 35 * 24 * H
    aged = wm_start + H
    oracle = {_LAST_CLOSED: (10, 5), aged: (7, 4)}
    agg = {_LAST_CLOSED: (10, 5), aged: (2, 4)}             # agg corrupted on the deep-catchup hour
    with pytest.raises(RuntimeError, match="served-value gate FAILED"):
        _run(oracle, agg, {_LAST_CLOSED: 10, aged: 7}, _WM(start=wm_start))


def test_long_backlog_drains_one_chunk_per_run():
    # A 10-day backlog must advance by ONE bounded chunk (not jump to last_closed), so each run's CH scan stays
    # under the 60s timeout; the rest drains on subsequent runs.
    wm_start = _LAST_CLOSED - 10 * 24 * H
    win_start = wm_start - v._LATENESS_S
    in_chunk = win_start + H
    oracle = {in_chunk: (5, 3)}
    wm = _WM(start=wm_start)
    out = _run(oracle, dict(oracle), {in_chunk: 5}, wm)
    assert out["all_ok"]
    assert out["window"][1] == win_start + v._CATCHUP_CHUNK_S    # upper bound capped at the chunk, not cutoff
    assert wm.value == win_start + v._CATCHUP_CHUNK_S - H         # advanced one chunk boundary only
    assert wm.value < _LAST_CLOSED


def test_empty_window_advances_without_mismatch():
    wm = _WM(start=None)
    out = _run({}, {}, {}, wm)
    assert out["all_ok"]
    assert wm.value == _LAST_CLOSED                     # nothing to validate -> still advance the forward guard


def test_window_uses_closed_cutoff_and_lateness_geo_metric():
    # The oracle SQL must carry the MV's geo filter + the dedup-immune metric, scoped to [win_start, cutoff).
    captured = {}

    def q(sql):
        # The opensky oracle only (the airline adsb oracle also JOINs bronze.opensky_states for the backfill).
        if "bronze.opensky_states" in sql and "bronze.adsb_states" not in sql:
            captured["oracle"] = sql
        return []
    wm = _WM(start=None)
    v.run_value_gate(ch_query=q, get_wm=wm.get, set_wm=wm.set, now_epoch=_NOW)
    o = captured["oracle"]
    assert "latitude BETWEEN 20 AND 50 AND longitude BETWEEN 122 AND 165" in o
    assert "uniqExact((icao24, snapshot_time))" in o
    assert str(_CUTOFF) in o                            # closed boundary on the upper bound
    assert str(_LAST_CLOSED - v._LATENESS_S) in o       # lateness tail on the lower bound (wm is None -> anchor=last_closed)


# --- ADS-B served-value checks (the two re-grained MVs, exact vs the bronze.adsb_states oracle) ------------------
def test_adsb_country_and_airline_pass():
    oracle = {_LAST_CLOSED: (10, 5)}
    cc = {("Japan", _LAST_CLOSED): (4, 9, 1), ("United States", _LAST_CLOSED): (2, 3, 0)}
    aa = {("ANA", "Japan", _LAST_CLOSED): (3, 6, 2)}   # (distinct, observations, backfilled)
    wm = _WM(start=None)
    out = _run(oracle, dict(oracle), {_LAST_CLOSED: 10}, wm,
               cc_oracle=cc, cc_mart=dict(cc), aa_oracle=aa, aa_mart=dict(aa))
    assert out["all_ok"] and out["total"] == 4
    assert wm.value == _LAST_CLOSED


def test_adsb_country_observations_mismatch_reds():
    oracle = {_LAST_CLOSED: (10, 5)}
    cc_o = {("Japan", _LAST_CLOSED): (4, 9, 1)}
    cc_m = {("Japan", _LAST_CLOSED): (4, 8, 1)}                       # one observation short
    wm = _WM(start=None)
    with pytest.raises(RuntimeError, match="agg_country_traffic_adsb"):
        _run(oracle, dict(oracle), {_LAST_CLOSED: 10}, wm, cc_oracle=cc_o, cc_mart=cc_m)
    assert wm.sets == []                                             # watermark frozen on a defect


def test_adsb_country_military_mismatch_reds():
    # the baked-db_flags military path: oracle decodes 2 military, the mart serves 1.
    oracle = {_LAST_CLOSED: (10, 5)}
    cc_o = {("Japan", _LAST_CLOSED): (4, 9, 2)}
    cc_m = {("Japan", _LAST_CLOSED): (4, 9, 1)}
    with pytest.raises(RuntimeError, match="agg_country_traffic_adsb"):
        _run(oracle, dict(oracle), {_LAST_CLOSED: 10}, _WM(start=None), cc_oracle=cc_o, cc_mart=cc_m)


def test_adsb_phantom_country_reds():
    # a country present in the mart but absent in the oracle (mart>0, oracle 0) is a defect, not just a drop.
    oracle = {_LAST_CLOSED: (10, 5)}
    cc_m = {("Phantomland", _LAST_CLOSED): (1, 1, 0)}
    with pytest.raises(RuntimeError, match="agg_country_traffic_adsb"):
        _run(oracle, dict(oracle), {_LAST_CLOSED: 10}, _WM(start=None), cc_mart=cc_m)


def test_adsb_airline_observations_mismatch_reds():
    oracle = {_LAST_CLOSED: (10, 5)}
    aa_o = {("ANA", "Japan", _LAST_CLOSED): (3, 6, 2)}
    aa_m = {("ANA", "Japan", _LAST_CLOSED): (3, 5, 2)}               # observations short of bronze
    with pytest.raises(RuntimeError, match="agg_airline_traffic_adsb"):
        _run(oracle, dict(oracle), {_LAST_CLOSED: 10}, _WM(start=None), aa_oracle=aa_o, aa_mart=aa_m)


def test_adsb_airline_backfilled_and_distinct_mismatch_reds():
    # P2: the OpenSky-backfilled count AND distinct_aircraft are now gated (not just native observations).
    oracle = {_LAST_CLOSED: (10, 5)}
    aa_o = {("ANA", "Japan", _LAST_CLOSED): (3, 6, 2)}
    for bad in ({("ANA", "Japan", _LAST_CLOSED): (3, 6, 1)},        # backfilled diverges
                {("ANA", "Japan", _LAST_CLOSED): (2, 6, 2)}):       # distinct_aircraft diverges
        with pytest.raises(RuntimeError, match="agg_airline_traffic_adsb"):
            _run(oracle, dict(oracle), {_LAST_CLOSED: 10}, _WM(start=None), aa_oracle=aa_o, aa_mart=bad)


def test_adsb_checks_skipped_below_ttl_floor():
    # A deep catch-up below the 89-day adsb TTL floor: the _acc has aged the hours out by design, so the adsb
    # checks must be SKIPPED (not red on an empty mart while the unbounded bronze oracle still holds the hour).
    wm_start = _LAST_CLOSED - 95 * 24 * H
    oracle = {_LAST_CLOSED: (10, 5), wm_start + H: (7, 4)}
    cc_o = {("Japan", wm_start + H): (3, 5, 0)}                      # bronze still has it; the mart aged it out
    # fss now validates full history (no floor), so its dict must hold the aged hour too, isolating the adsb skip.
    out = _run(oracle, dict(oracle), {_LAST_CLOSED: 10, wm_start + H: 7}, _WM(start=wm_start), cc_oracle=cc_o)
    assert out["all_ok"]
    assert not any("adsb" in r["check"] for r in out["results"])     # adsb checks skipped, not failed


# --- watermark store: advance-only, portable (sqlite mirror of the postgres table) ------------------------------
def test_watermark_store_is_advance_only(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "_WM_TABLE", "ch_served_value_audit")  # schema-less for the sqlite mirror
    eng = sa.create_engine(f"sqlite:///{tmp_path}/wm.db")
    v._ensure_table(engine=eng)
    assert v.get_watermark(gate="t", engine=eng) is None
    v.set_watermark(100, gate="t", engine=eng)
    assert v.get_watermark(gate="t", engine=eng) == 100
    v.set_watermark(90, gate="t", engine=eng)          # lower -> ignored
    assert v.get_watermark(gate="t", engine=eng) == 100
    v.set_watermark(200, gate="t", engine=eng)         # higher -> advances
    assert v.get_watermark(gate="t", engine=eng) == 200
