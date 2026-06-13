"""Integration test for maintain_iceberg_marts' maintenance operations.

Runs optimize/expire/orphans (the statements the DAG issues) against a throwaway
silver table so it can't race transform_marts' 12-min REPLACE of the real marts.
Skips when trino-coordinator isn't reachable. The production "snapshot count
drops to <=840 after expire" check is a soak/DAG-trigger acceptance, not unit-
testable here: Trino enforces a 7d min-retention so fresh snapshots can't expire.
"""

from __future__ import annotations

import pytest


TABLE = "iceberg.silver._mainttest"


def _run(cur, sql: str):
    cur.execute(sql)
    return cur.fetchall()


def _scalar(cur, sql: str) -> int:
    return _run(cur, sql)[0][0]


def _meta_count(cur, suffix: str) -> int:  # count rows in a "$files"/"$snapshots" metatable
    return _scalar(cur, f'SELECT count(*) FROM iceberg.silver."_mainttest{suffix}"')


def test_maintenance_ops_compact_and_run_clean(cur):
    try:
        _run(cur, f"DROP TABLE IF EXISTS {TABLE}")
    except Exception as exc:
        pytest.skip(f"silver schema not reachable: {exc}")

    _run(cur, f"CREATE TABLE {TABLE} (a integer) WITH (format = 'PARQUET')")
    try:
        # Separate inserts => one data file + one snapshot each.
        for i in range(4):
            _run(cur, f"INSERT INTO {TABLE} VALUES ({i})")

        files_before = _meta_count(cur, "$files")
        assert files_before >= 4, f"expected >=4 small files, got {files_before}"

        # optimize compacts the small files into fewer.
        _run(cur, f"ALTER TABLE {TABLE} EXECUTE optimize")
        files_after = _meta_count(cur, "$files")
        assert files_after < files_before, (
            f"optimize did not compact: before={files_before} after={files_after}"
        )

        # Measure snapshots after optimize: expire must be non-increasing here,
        # since fresh snapshots can't drop under the 7d min retention.
        snaps_pre_expire = _meta_count(cur, "$snapshots")
        _run(cur, f"ALTER TABLE {TABLE} EXECUTE expire_snapshots(retention_threshold => '7d')")
        snaps_after = _meta_count(cur, "$snapshots")
        assert snaps_after <= snaps_pre_expire, (
            f"expire increased snapshot count: before={snaps_pre_expire} after={snaps_after}"
        )
        _run(cur, f"ALTER TABLE {TABLE} EXECUTE remove_orphan_files(retention_threshold => '7d')")

        # Data is intact after all three maintenance ops.
        assert _scalar(cur, f"SELECT count(*) FROM {TABLE}") == 4
    finally:
        _run(cur, f"DROP TABLE IF EXISTS {TABLE}")
