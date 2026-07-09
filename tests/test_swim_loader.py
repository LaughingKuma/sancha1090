import polars as pl
import pytest
from include import bronze_transforms as bt
from include import clickhouse as ch
from include.swim_consumer import _BRONZE_COLS


def test_transform_swim_frame_projects_full_contract():
    df = pl.DataFrame({c: [None] for c in _BRONZE_COLS})
    out = bt.transform_swim_frame(df)
    assert out.columns == list(_BRONZE_COLS)   # exact order, nothing dropped or invented


def test_transform_swim_frame_fails_loud_on_missing_columns():
    # a drifted frame must raise, not insert with silently-NULL columns.
    df = pl.DataFrame({"acid": ["JAL551"], "dep_point": ["RJTT"], "raw_xml": ["<x/>"]})
    with pytest.raises(ValueError, match="missing bronze columns"):
        bt.transform_swim_frame(df)


def test_load_swim_empty_is_ok(monkeypatch):
    # Hermetic: stub the manifest so no analytics-Postgres round-trip; empty pending → ok True, files 0.
    from include import manifest
    monkeypatch.setattr(manifest, "pending_ch_uris", lambda _prefix, _engine=None: [])
    res = ch.load_swim_pending_to_ch()
    assert res["ok"] is True
    assert res["files"] == 0


def test_swim_bronze_cols_reuse_drain_module_source_of_truth():
    # bronze_transforms must REUSE the drain module's canonical list, not a copy — guards against re-duplication.
    assert bt._SWIM_BRONZE_COLS is _BRONZE_COLS
