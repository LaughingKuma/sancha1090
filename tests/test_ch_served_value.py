import pytest
import sqlalchemy as sa

from include import ch_served_value as v

H = 3600
_NOW = 1_780_000_000
_CUTOFF = _NOW // H * H - v._CLOSED_WINDOW_S
_LAST_CLOSED = _CUTOFF - H


# Drives the gate with no live CH: each query is routed by its table token (mirrors test_ch_parity_source_gate).
def _ch_fake(oracle, agg, fss):
    def q(sql):
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


def _run(oracle, agg, fss, wm):
    return v.run_value_gate(ch_query=_ch_fake(oracle, agg, fss),
                            get_wm=wm.get, set_wm=wm.set, now_epoch=_NOW)


def test_value_gate_passes_and_advances_watermark():
    oracle = {_LAST_CLOSED: (10, 5), _LAST_CLOSED - H: (8, 4)}
    wm = _WM(start=None)
    out = _run(oracle, dict(oracle), {_LAST_CLOSED: 10, _LAST_CLOSED - H: 8}, wm)
    assert out["all_ok"]
    assert out["passed"] == out["total"] == 2          # agg_hourly + fact_state_snapshots
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


def test_fss_retention_floor_skips_aged_out_hours():
    # After an outage > fss's 30-day window, the oldest held hours are gone from fact_state_snapshots by design;
    # the gate must not red on fss==0 there, while agg_hourly (accumulate-forever) still validates cleanly.
    wm_start = _LAST_CLOSED - 35 * 24 * H
    aged = wm_start + H                                     # ~35 days back: below the 29-day fss floor
    oracle = {_LAST_CLOSED: (10, 5), aged: (7, 4)}
    out = _run(oracle, dict(oracle), {_LAST_CLOSED: 10}, _WM(start=wm_start))   # fss holds nothing for `aged`
    assert out["all_ok"]


def test_agg_still_validated_below_fss_floor():
    # agg_hourly has no retention floor, so a discrepancy in a hour below the fss window must STILL red.
    wm_start = _LAST_CLOSED - 35 * 24 * H
    aged = wm_start + H
    oracle = {_LAST_CLOSED: (10, 5), aged: (7, 4)}
    agg = {_LAST_CLOSED: (10, 5), aged: (2, 4)}             # agg corrupted below the fss floor
    with pytest.raises(RuntimeError, match="served-value gate FAILED"):
        _run(oracle, agg, {_LAST_CLOSED: 10}, _WM(start=wm_start))


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
        if "bronze.opensky_states" in sql:
            captured["oracle"] = sql
        return []
    wm = _WM(start=None)
    v.run_value_gate(ch_query=q, get_wm=wm.get, set_wm=wm.set, now_epoch=_NOW)
    o = captured["oracle"]
    assert "latitude BETWEEN 20 AND 50 AND longitude BETWEEN 122 AND 165" in o
    assert "uniqExact((icao24, snapshot_time))" in o
    assert str(_CUTOFF) in o                            # closed boundary on the upper bound
    assert str(_LAST_CLOSED - v._LATENESS_S) in o       # lateness tail on the lower bound (wm is None -> anchor=last_closed)


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
