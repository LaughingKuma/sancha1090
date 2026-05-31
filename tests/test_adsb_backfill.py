from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq

from include import adsb_backfill as bf


SAMPLE_PARQUET = Path(__file__).resolve().parent / "fixtures" / "adsb" / "sample_adsb_state.parquet"


# Each case mirrors a documented capture_v2._coerce quirk — backfill rows must be byte-comparable
# to live bronze, so the coercion has to match the producer exactly.

def test_coerce_none_passes_through():
    assert bf.coerce("gs", None) is None


def test_coerce_string_keeps_str_and_stringifies_others():
    assert bf.coerce("flight", "AAL280  ") == "AAL280  "   # padding preserved
    assert bf.coerce("alt_baro", 35000) == "35000"          # int → str
    assert bf.coerce("alt_baro", "ground") == "ground"      # sentinel passes through


def test_coerce_double_accepts_numbers_rejects_bool_and_bad():
    assert bf.coerce("lat", 35.6) == 35.6
    assert bf.coerce("gs", 312) == 312.0                    # int → float
    assert isinstance(bf.coerce("gs", 312), float)
    assert bf.coerce("gs", True) is None                    # bool is not a real double
    assert bf.coerce("gs", "fast") is None                  # unparseable → None


def test_coerce_int_integral_only():
    assert bf.coerce("messages", 312) == 312
    assert bf.coerce("messages", 312.0) == 312              # integral float → int
    assert bf.coerce("messages", 312.5) is None             # non-integral → None
    assert bf.coerce("messages", True) == 1                 # bool → int
    assert bf.coerce("messages", "5") is None               # str → None


def test_coerce_list_of_strings():
    assert bf.coerce("nav_modes", ["autopilot", "tcas"]) == ["autopilot", "tcas"]
    assert bf.coerce("nav_modes", [1, 2]) == ["1", "2"]     # elements stringified
    assert bf.coerce("nav_modes", "autopilot") is None      # non-list → None


def test_coerce_json_field_serializes_compactly():
    assert bf.coerce("acas_ra", {"AP": 1, "x": [2]}) == json.dumps({"AP": 1, "x": [2]}, separators=(",", ":"))


def test_coerce_unknown_field_is_dropped():
    assert bf.coerce("dbFlags", 8) is None                   # untyped → not a column (lives in _raw_json)


def _record():
    return {
        "capture_ts": 1779532053.65,
        "msg": {
            "hex": "a1b2c3", "flight": "AAL280  ", "alt_baro": "ground", "gs": 312,
            "messages": 1500.0, "nav_modes": ["autopilot"], "acas_ra": {"AP": 1},
            "dbFlags": 8,  # untyped readsb field — must survive only in _raw_json
        },
    }


def test_record_to_row_has_exactly_the_schema_columns():
    from include.adsb_iceberg import ADSB_SCHEMA
    row = bf.record_to_row(_record())
    assert list(row.keys()) == [f.name for f in ADSB_SCHEMA.fields]
    assert len(row) == 60


def test_record_to_row_types_and_meta_columns():
    row = bf.record_to_row(_record())
    assert row["capture_ts"] == 1779532053.65
    assert row["hex"] == "a1b2c3"
    assert row["flight"] == "AAL280  "       # padding preserved
    assert row["alt_baro"] == "ground"
    assert row["gs"] == 312.0
    assert row["messages"] == 1500           # integral float → int
    assert row["nav_modes"] == ["autopilot"]
    assert row["_schema_version"] == 1


def test_record_to_row_preserves_untyped_field_in_raw_json_only():
    import json
    row = bf.record_to_row(_record())
    assert "dbFlags" not in row              # not promoted to a column
    raw = json.loads(row["_raw_json"])
    assert raw["dbFlags"] == 8               # but losslessly present in _raw_json


def test_backfill_parquet_schema_matches_producer():
    # Backfill Parquet must be structurally identical to live producer output, so add_files
    # treats both the same. Pin it to the real producer fixture.
    assert bf.PA_SCHEMA.equals(pq.read_schema(SAMPLE_PARQUET))


def test_utc_hour_str_floors_to_hour():
    # 1779532053.65 == 2026-05-23T10:27:33Z (the .gz filename's 19:27 is JST) → bucket "2026-05-23T10"
    assert bf.utc_hour_str(1779532053.65) == "2026-05-23T10"


def test_group_records_by_hour_flushes_on_hour_change():
    recs = [
        {"capture_ts": 1779532053.0, "msg": {"hex": "a"}},   # 10:27 UTC
        {"capture_ts": 1779532100.0, "msg": {"hex": "b"}},   # 10:28 UTC
        {"capture_ts": 1779535660.0, "msg": {"hex": "c"}},   # 11:27 UTC
    ]
    groups = [(hour, [r["msg"]["hex"] for r in recs_in]) for hour, recs_in in bf.group_records_by_hour(recs)]
    assert groups == [
        ("2026-05-23T10", ["a", "b"]),
        ("2026-05-23T11", ["c"]),
    ]


def test_iter_records_skips_blank_malformed_and_non_records():
    lines = [
        '{"capture_ts": 1.0, "msg": {"hex": "a"}}',
        "",                                   # blank
        "   ",                                # whitespace
        "{not json",                          # malformed
        "[1,2,3]",                            # not a dict
        '{"msg": {}}',                        # no capture_ts
        '{"capture_ts": 2.0, "msg": {"hex": "b"}}',
    ]
    got = [r["msg"]["hex"] for r in bf.iter_records(lines)]
    assert got == ["a", "b"]


def test_backfill_records_skips_hours_at_or_after_the_live_bound():
    recs = [
        {"capture_ts": 1779532053.0, "msg": {"hex": "a"}},   # 2026-05-23T10 → backfill
        {"capture_ts": 1779937200.0, "msg": {"hex": "b"}},   # 2026-05-28T03 → already live, skip
    ]
    calls = []

    def write_hour(hour, _rows):
        calls.append(hour)
        return {"s3_uri": f"s3://b/{hour}.parquet", "filename": f"{hour}.parquet"}

    out = bf.backfill_records(recs, end_before_hour="2026-05-28T03", write_hour_fn=write_hour)
    assert calls == ["2026-05-23T10"]               # the 05-28T03 hour is skipped (overlap)
    assert out == [{"hour": "2026-05-23T10", "rows": 1,
                    "s3_uri": "s3://b/2026-05-23T10.parquet", "filename": "2026-05-23T10.parquet"}]


def test_write_hour_parquet_roundtrips(tmp_path):
    rows = [bf.record_to_row(_record()), bf.record_to_row(_record())]
    out = tmp_path / "h.parquet"
    bf.write_hour_parquet(rows, str(out))
    t = pq.read_table(out)
    assert t.num_rows == 2
    assert t.schema.equals(bf.PA_SCHEMA)
    assert t.column("flight").to_pylist() == ["AAL280  ", "AAL280  "]
