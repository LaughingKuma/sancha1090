from __future__ import annotations

import pytest


def _catalog_or_skip():
    try:
        from include import iceberg as ib

        catalog = ib.get_catalog()
        catalog.load_table(ib.QUALIFIED)
    except Exception as exc:
        pytest.skip(f"polaris not reachable from this host: {exc}")
    return ib, catalog


def test_get_catalog_is_polaris_rest():
    from pyiceberg.catalog.rest import RestCatalog

    ib, catalog = _catalog_or_skip()
    assert isinstance(catalog, RestCatalog), (
        "get_catalog() must return a Polaris RestCatalog after v2.4-5"
    )


def test_tableize_increments_polaris_snapshot_count():
    import pyarrow as pa
    from pyiceberg.exceptions import NoSuchTableError

    ib, catalog = _catalog_or_skip()

    # Throwaway table: appending to prod bronze.opensky_states would leak a
    # null/epoch-0 partition row into the live marts + deck.gl map.
    ident = "bronze.sigtest_primary_writer"
    try:
        catalog.drop_table(ident)
    except NoSuchTableError:
        pass

    table = catalog.create_table(
        identifier=ident, schema=ib.SCHEMA, partition_spec=ib.PARTITION_SPEC
    )
    try:
        before = len(table.snapshots())

        arrow_schema = table.schema().as_arrow()
        columns = {
            f.name: (
                pa.array([0], type=pa.int64()).cast(f.type)
                if f.name == "snapshot_time"
                else pa.nulls(1, type=f.type)
            )
            for f in arrow_schema
        }
        # Server-side commit through Polaris is the v2.4-5 primary-writer path.
        table.append(pa.table(columns), snapshot_properties={"manifest_fingerprint": "primary-writer-test"})
        table.refresh()
        after = len(table.snapshots())

        assert after == before + 1, (
            f"Polaris snapshot count should grow by exactly 1; before={before} after={after}"
        )
    finally:
        catalog.purge_table(ident)
