from __future__ import annotations

import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from include.adsb_schema import (
    ADSB_COLUMNS,
    DOUBLE_FIELDS,
    INT_FIELDS,
    JSON_FIELDS,
    LIST_FIELDS,
    STRING_FIELDS,
)


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "adsb"
SAMPLE_PARQUET = FIXTURE_DIR / "sample_adsb_state.parquet"
BRONZE_SQL = Path(__file__).resolve().parents[1] / "clickhouse" / "sql" / "01_bronze.sql"


def _bronze_adsb_ddl_columns() -> list[str]:
    # Parse the bronze.adsb_states column names (in order) out of the CH DDL. Columns are packed
    # multiple-per-line with -- comments and backtick-quoted reserved words; the trailing MATERIALIZED
    # partition-driver column is computed (not part of the positional load) so it's excluded.
    ddl = BRONZE_SQL.read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE IF NOT EXISTS bronze\.adsb_states\s*\((.*?)\)\s*ENGINE", ddl, re.S)
    assert m, "could not locate bronze.adsb_states DDL in clickhouse/sql/01_bronze.sql"
    cols = []
    for chunk in m.group(1).split(","):
        line = re.sub(r"--[^\n]*", "", chunk).strip()  # drop end-of-line comments
        if not line or "MATERIALIZED" in line.upper():
            continue
        cols.append(line.split()[0].strip("`"))
    return cols


def test_producer_parquet_matches_adsb_columns():
    """The bronze.adsb_states column contract must mirror the edge producer's
    capture_v2._build_schema() exactly — drift in EITHER direction (added/removed/renamed/
    reordered column) fails here, since the per-tick CH load inserts the Parquet by position."""
    producer = pq.read_schema(SAMPLE_PARQUET)
    assert len(ADSB_COLUMNS) == len(producer.names) == 60, (
        f"column count drift: schema={len(ADSB_COLUMNS)} producer={len(producer.names)}"
    )
    assert ADSB_COLUMNS == list(producer.names), (
        "column name/order drift between the adsb_schema contract and the producer parquet"
    )


def test_producer_parquet_column_types_match_buckets():
    """Per-column type contract: the per-tick CH load hands the producer Arrow types straight into the
    fixed-type bronze.adsb_states DDL, so a producer-side TYPE drift (e.g. a DOUBLE_FIELD arriving as int)
    must fail here before it reaches the loader and silently coerces or rejects."""
    schema = pq.read_schema(SAMPLE_PARQUET)
    types = {f.name: f.type for f in schema}

    def assert_type(name, ok):
        assert ok, f"type drift on {name!r}: producer={types[name]}"

    assert_type("capture_ts", pa.types.is_float64(types["capture_ts"]))
    assert_type("_raw_json", pa.types.is_string(types["_raw_json"]))
    assert_type("_schema_version", pa.types.is_integer(types["_schema_version"]))
    for n in STRING_FIELDS + JSON_FIELDS:
        assert_type(n, pa.types.is_string(types[n]))
    for n in DOUBLE_FIELDS:
        assert_type(n, pa.types.is_float64(types[n]))
    for n in INT_FIELDS:
        assert_type(n, pa.types.is_integer(types[n]))  # Int64 column accepts int32/int64
    for n in LIST_FIELDS:
        assert_type(n, pa.types.is_list(types[n]) and pa.types.is_string(types[n].value_type))

    # Byte-mirror contract: every producer column is nullable (capture_v2 declares all columns nullable).
    assert all(f.nullable for f in schema), "a producer column is unexpectedly non-nullable"


def test_bronze_sql_ddl_matches_adsb_columns():
    """Third leg of the contract: the per-tick load inserts the producer Parquet POSITIONALLY into
    bronze.adsb_states, so the CH DDL column order must equal ADSB_COLUMNS (== the producer parquet)."""
    assert _bronze_adsb_ddl_columns() == ADSB_COLUMNS, (
        "bronze.adsb_states DDL column order drift vs ADSB_COLUMNS (clickhouse/sql/01_bronze.sql)"
    )
