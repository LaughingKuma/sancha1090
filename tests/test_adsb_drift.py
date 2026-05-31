from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow.fs import LocalFileSystem

from dags import maintain_adsb_schema as ms
from include import adsb_drift as dr


class FakeLog:
    """Captures error() calls so a test can assert whether the drift alert fired."""

    def __init__(self):
        self.errors: list = []

    def error(self, *a, **k):
        self.errors.append((a, k))

    def info(self, *a, **k):
        pass


def _write_parquet(path, raw_dicts: list[dict]):
    raw = [json.dumps(d) for d in raw_dicts]
    pq.write_table(pa.table({"_raw_json": pa.array(raw, type=pa.string())}), str(path))


def _batch(raw_values: list[str | None]) -> pa.RecordBatch:
    return pa.record_batch({"_raw_json": pa.array(raw_values, type=pa.string())})


def test_flags_brand_new_field():
    new = dr.find_new_untyped_fields({"gpsOkBefore", "hex", "lat"}, dr.KNOWN_UNTYPED)
    assert new == {"gpsOkBefore"}


def test_typed_field_not_flagged():
    assert dr.find_new_untyped_fields({"squawk"}, dr.KNOWN_UNTYPED) == set()


def test_known_untyped_suppressed():
    assert dr.find_new_untyped_fields({"dbFlags", "calc_track"}, dr.KNOWN_UNTYPED) == set()


def test_empty_observed_is_clean():
    assert dr.find_new_untyped_fields(set(), dr.KNOWN_UNTYPED) == set()


def test_typed_set_excludes_our_own_columns():
    # capture_ts/_raw_json/_schema_version are ours, not readsb fields — they must not be typed,
    # otherwise the scan would never notice if they vanished, and they'd mask real readsb keys.
    for ours in ("capture_ts", "_raw_json", "_schema_version"):
        assert ours not in dr.TYPED_READSB_FIELDS


def test_count_keys_returns_union_and_parsed_count():
    batches = [_batch([
        json.dumps({"hex": "abc", "lat": 1.0}),
        json.dumps({"hex": "def", "dbFlags": 8}),
    ])]
    seen, parsed = dr.count_raw_json_keys(batches, sample_rows=200_000)
    assert parsed == 2
    assert set(seen) == {"hex", "lat", "dbFlags"}
    assert seen["hex"] == 2


def test_count_keys_respects_sample_cap():
    batches = [_batch([json.dumps({"hex": "a"}), json.dumps({"lat": 1.0}),
                       json.dumps({"calc_track": 9})])]
    seen, parsed = dr.count_raw_json_keys(batches, sample_rows=2)
    assert parsed == 2
    assert set(seen) == {"hex", "lat"}  # third row never parsed


def test_count_keys_skips_null_and_malformed_rows():
    batches = [_batch([None, "not json", json.dumps({"hex": "a"})])]
    seen, parsed = dr.count_raw_json_keys(batches, sample_rows=200_000)
    assert parsed == 1
    assert set(seen) == {"hex"}


def test_scan_core_flags_new_field_and_suppresses_known(tmp_path):
    root = tmp_path / "bronze" / "adsb_state" / "dt=2026-05-30"
    root.mkdir(parents=True)
    _write_parquet(root / "edge_adsb_state_2026-05-30T00.parquet",
                   [{"hex": "a", "lat": 1.0}])
    _write_parquet(root / "edge_adsb_state_2026-05-30T01.parquet",
                   [{"hex": "b", "dbFlags": 8, "gpsOkBefore": 1}])

    log = FakeLog()
    summary = ms.scan_core(LocalFileSystem(), str(tmp_path / "bronze" / "adsb_state"),
                           limit_files=0, sample_rows=200_000, log=log)

    assert summary["new_fields"] == ["gpsOkBefore"]
    assert summary["suppressed"] == ["dbFlags"]  # known-untyped seen but not alerted
    assert summary["files"] == 2
    assert summary["rows_parsed"] == 2
    assert log.errors  # alert fired on the genuinely new field


def test_scan_core_clean_when_only_typed_and_known(tmp_path):
    root = tmp_path / "bronze" / "adsb_state" / "dt=2026-05-30"
    root.mkdir(parents=True)
    _write_parquet(root / "edge_adsb_state_2026-05-30T00.parquet",
                   [{"hex": "a", "squawk": "1200", "dbFlags": 8, "calc_track": 90.0}])

    log = FakeLog()
    summary = ms.scan_core(LocalFileSystem(), str(tmp_path / "bronze" / "adsb_state"),
                           limit_files=0, sample_rows=200_000, log=log)

    assert summary["new_fields"] == []
    assert not log.errors  # no alert when every key is typed or on the allowlist


def test_scan_core_limits_to_most_recent_files(tmp_path):
    root = tmp_path / "bronze" / "adsb_state" / "dt=2026-05-30"
    root.mkdir(parents=True)
    _write_parquet(root / "edge_adsb_state_2026-05-30T00.parquet", [{"oldOnlyField": 1}])
    _write_parquet(root / "edge_adsb_state_2026-05-30T01.parquet", [{"hex": "b", "marker": "seen"}])

    log = FakeLog()
    summary = ms.scan_core(LocalFileSystem(), str(tmp_path / "bronze" / "adsb_state"),
                           limit_files=1, sample_rows=200_000, log=log)

    # Only the newest file scanned: its distinctive key surfaces, the older file's never does.
    assert summary["files"] == 1
    assert "marker" in summary["new_fields"]
    assert "oldOnlyField" not in summary["new_fields"]
