from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pyiceberg.types import (
    DoubleType,
    IntegerType,
    ListType,
    LongType,
    StringType,
)

from include import adsb_iceberg as ai


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "adsb"
SAMPLE_PARQUET = FIXTURE_DIR / "sample_adsb_state.parquet"


def _expected_iceberg_type(pa_type: pa.DataType):
    """The spec's §3 PyArrow→Iceberg mapping, as a predicate over an Iceberg type."""
    if pa.types.is_float64(pa_type):
        return lambda t: isinstance(t, DoubleType)
    if pa.types.is_int64(pa_type):
        return lambda t: isinstance(t, LongType)
    if pa.types.is_int32(pa_type):
        return lambda t: isinstance(t, IntegerType)
    if pa.types.is_string(pa_type):
        return lambda t: isinstance(t, StringType)
    if pa.types.is_list(pa_type) and pa.types.is_string(pa_type.value_type):
        return lambda t: isinstance(t, ListType) and isinstance(t.element_type, StringType)
    raise AssertionError(f"unmapped producer type: {pa_type}")


def test_producer_parquet_matches_table_schema():
    """Bronze DDL must mirror capture_v2._build_schema() exactly — drift in EITHER
    direction (added/removed/renamed/retyped column) fails here."""
    producer = pq.read_schema(SAMPLE_PARQUET)
    iceberg_fields = ai.ADSB_SCHEMA.fields

    assert len(iceberg_fields) == len(producer.names) == 60, (
        f"column count drift: iceberg={len(iceberg_fields)} producer={len(producer.names)}"
    )

    assert [f.name for f in iceberg_fields] == list(producer.names), (
        "column name/order drift between bronze schema and producer parquet"
    )

    for ice_field, pa_field in zip(iceberg_fields, producer, strict=True):
        predicate = _expected_iceberg_type(pa_field.type)
        assert predicate(ice_field.field_type), (
            f"type drift on {ice_field.name}: iceberg={ice_field.field_type} "
            f"producer={pa_field.type}"
        )
        # Nullability must match too — byte-mirror add_files rejects a required Iceberg
        # column fed by a nullable Parquet column.
        assert ice_field.required == (not pa_field.nullable), (
            f"nullability drift on {ice_field.name}: iceberg.required={ice_field.required} "
            f"producer.nullable={pa_field.nullable}"
        )


def test_all_columns_nullable_matching_producer():
    """capture_v2._build_schema() declares every column nullable, so the bronze DDL must too:
    byte-mirror add_files cannot promote a nullable Parquet column to a required Iceberg one.
    The capture_ts/_schema_version producer invariants hold at the data level, not the column
    level — enforcing non-null would be a producer-side _schema_version bump (v4.x)."""
    assert all(not f.required for f in ai.ADSB_SCHEMA.fields)


def test_ensure_adsb_namespace_and_table_is_idempotent(local_catalog):
    t1 = ai.ensure_adsb_namespace_and_table(local_catalog)
    t2 = ai.ensure_adsb_namespace_and_table(local_catalog)
    assert local_catalog.table_exists(ai.QUALIFIED)
    assert [f.name for f in t1.schema().fields] == [f.name for f in t2.schema().fields]


def test_add_files_happy_path(local_catalog):
    table = ai.ensure_adsb_namespace_and_table(local_catalog)
    path = str(SAMPLE_PARQUET)

    result = ai.add_files_to_adsb(table, [path])

    assert list(result.keys()) == [path]
    assert isinstance(result[path], int)
    assert table.scan().to_arrow().num_rows == 5


def test_add_files_idempotent_after_partial_commit(local_catalog):
    """The mandatory reconciliation test: add_files succeeded but mark_committed crashed.
    Replaying the same batch must NOT double-add and must return the original snapshot_id
    per path."""
    table = ai.ensure_adsb_namespace_and_table(local_catalog)
    path = str(SAMPLE_PARQUET)

    first = ai.add_files_to_adsb(table, [path])

    # Simulate the crash-then-replay: reload the table and re-run the identical batch.
    table = local_catalog.load_table(ai.QUALIFIED)
    second = ai.add_files_to_adsb(table, [path])

    assert second == first  # same {path: snapshot_id}
    table = local_catalog.load_table(ai.QUALIFIED)
    assert table.scan().to_arrow().num_rows == 5  # not 10 — the file is registered exactly once


def test_add_files_raises_when_path_unattributable_to_snapshot(local_catalog, monkeypatch):
    """If a present path's adding snapshot was expired out of history, fail loudly rather than
    return a dict missing that key (which the caller would index into with a KeyError)."""
    table = ai.ensure_adsb_namespace_and_table(local_catalog)
    path = str(SAMPLE_PARQUET)
    ai.add_files_to_adsb(table, [path])
    table = local_catalog.load_table(ai.QUALIFIED)

    # Simulate the adding snapshot no longer being in history.
    monkeypatch.setattr(ai, "_paths_added_in_snapshot", lambda *_a, **_kw: set())
    with pytest.raises(RuntimeError, match="cannot attribute"):
        ai.add_files_to_adsb(table, [path])
