# v2.8: pg parity gate retired; guards the surviving invariant — Superset's main_dttm_col stays tz-aware on iceberg.gold/silver.

from __future__ import annotations

import pytest


# Superset's Trino dialect DATEADDs against these, so they MUST be TIMESTAMP WITH TIME ZONE.
MAIN_DTTM_COL = {
    "agg_country_traffic": "snapshot_ts",
    "agg_hourly_traffic": "snapshot_hour",
    "anomalies": "snapshot_time",
    "fact_state_snapshots": "snapshot_time",
}


def _canonical_type(dtype: str) -> str:
    d = dtype.lower().strip()
    if d.startswith("timestamp") and "with time zone" in d:
        return "timestamp with time zone"
    if d.startswith("timestamp"):
        return "timestamp"
    return d


def _trino_columns(cur, table: str, schema: str) -> dict[str, str]:
    cur.execute(
        "SELECT column_name, data_type FROM iceberg.information_schema.columns "
        "WHERE table_schema = ? AND table_name = ?",
        (schema, table),
    )
    rows = cur.fetchall()
    if not rows:
        pytest.skip(f"trino mart {schema}.{table} not built yet")
    return {r[0]: _canonical_type(r[1]) for r in rows}


@pytest.mark.parametrize("table,schema", [
    ("agg_country_traffic", "gold"),
    ("agg_hourly_traffic", "gold"),
    ("anomalies", "gold"),
    ("fact_state_snapshots", "silver"),
])
def test_main_dttm_col_is_tz_aware(cur, table, schema):
    cols = _trino_columns(cur, table, schema)
    dttm = MAIN_DTTM_COL[table]
    assert cols.get(dttm) == "timestamp with time zone", (
        f"{table}.{dttm} must be TIMESTAMP WITH TIME ZONE on iceberg.{schema}, "
        f"got {cols.get(dttm)!r} — would break Superset time-range filters"
    )
