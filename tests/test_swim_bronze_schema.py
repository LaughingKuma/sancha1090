# tests/test_swim_bronze_schema.py — STATIC guard over the DDL text (hermetic; no running ClickHouse needed).
import re
from pathlib import Path

from include.swim_consumer import _BRONZE_COLS

DDL = (Path(__file__).parents[1] / "clickhouse" / "sql" / "01_bronze.sql").read_text()

def _swim_block():
    m = re.search(r"CREATE TABLE IF NOT EXISTS bronze\.swim_flightdata.*?;", DDL, re.S)
    assert m, "bronze.swim_flightdata DDL not found"
    return m.group(0)

def test_order_by_excludes_volatile_and_keys_on_dedup_fp():
    order_by = re.search(r"ORDER BY \((.*?)\)", _swim_block(), re.S).group(1)
    assert "source_received_at" not in order_by and "ingested_at" not in order_by
    assert "_dedup_fp" in order_by

def test_has_expected_columns():
    block = _swim_block()
    for col in _BRONZE_COLS:
        assert col in block

def test_dedup_fp_hashes_raw_xml():
    # an amendment differing only in an unparsed XML field must still get a distinct fingerprint
    assert "cityHash64(raw_xml)" in _swim_block()
