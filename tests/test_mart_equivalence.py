"""v2.6 parity gate: dbt-trino silver/gold marts must match dbt-postgres marts.

Integration-marked and skip-on-not-reachable like the Polaris/Trino tests; they
need both analytics (postgres) and trino-coordinator on the compose network.
test_column_types_match is the hard gate for v2.7's Superset re-point.
"""

from __future__ import annotations

import os

import pytest


# main_dttm_col per dataset — these MUST be TIMESTAMP WITH TIME ZONE on Trino,
# the type Superset's Trino dialect macros DATEADD against in v2.7.
MAIN_DTTM_COL = {
    "agg_country_traffic": "snapshot_ts",
    "agg_hourly_traffic": "snapshot_hour",
    "anomalies": "snapshot_time",
    "fact_state_snapshots": "snapshot_time",
}


def _pg_query(sql: str, params: tuple = ()):  # skips if analytics unreachable
    try:
        import psycopg2
    except ImportError as exc:
        pytest.skip(f"psycopg2 not available: {exc}")
    try:
        conn = psycopg2.connect(
            host=os.environ.get("ANALYTICS_PG_HOST", "postgres-analytics"),
            port=int(os.environ.get("ANALYTICS_PG_PORT", "5432")),
            user=os.environ.get("ANALYTICS_PG_USER", "analytics"),
            password=os.environ.get("ANALYTICS_PG_PASSWORD", "analytics"),
            dbname=os.environ.get("ANALYTICS_PG_DB", "analytics"),
            connect_timeout=3,
        )
    except Exception as exc:
        pytest.skip(f"postgres-analytics not reachable: {exc}")
    try:
        with conn, conn.cursor() as cur:
            try:
                cur.execute(sql, params)
            except Exception as exc:
                pytest.skip(f"postgres mart not queryable yet: {exc}")
            return cur.fetchall()
    finally:
        conn.close()


def _trino_query(sql: str, params: tuple = ()):  # skips if trino unreachable
    try:
        import trino
    except ImportError as exc:
        pytest.skip(f"trino client not available: {exc}")
    try:
        conn = trino.dbapi.connect(
            host=os.environ.get("TRINO_HOST", "trino-coordinator"),
            port=int(os.environ.get("TRINO_PORT", "8080")),
            user="root",
            catalog="iceberg",
            http_scheme="http",
        )
        cur = conn.cursor()
        cur.execute(sql, params if params else None)
        return cur.fetchall()
    except Exception as exc:
        pytest.skip(f"trino mart not reachable/built yet: {exc}")


def _simplify(cols: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Normalize equivalent pg/trino type spellings to one canonical token."""
    out = set()
    for name, dtype in cols:
        d = dtype.lower().strip()
        if d.startswith("timestamp") and "with time zone" in d:
            t = "timestamp with time zone"
        elif d.startswith("timestamp"):
            t = "timestamp"
        elif d in ("double precision", "double"):
            t = "double"
        elif d.startswith(("numeric", "decimal")):
            t = "decimal"
        elif d in ("text", "character varying") or d.startswith("varchar"):
            t = "varchar"
        elif d in ("integer", "int", "int4"):
            t = "integer"
        elif d in ("bigint", "int8"):
            t = "bigint"
        elif d == "boolean":
            t = "boolean"
        else:
            t = d
        out.add((name, t))
    return out


def _pg_columns(table: str, schema: str) -> set[tuple[str, str]]:
    rows = _pg_query(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    )
    if not rows:
        pytest.skip(f"postgres mart {schema}.{table} not built yet")
    return {(r[0], r[1]) for r in rows}


def _trino_columns(table: str, schema: str) -> set[tuple[str, str]]:
    rows = _trino_query(
        "SELECT column_name, data_type FROM iceberg.information_schema.columns "
        "WHERE table_schema = ? AND table_name = ?",
        (schema, table),
    )
    if not rows:
        pytest.skip(f"trino mart {schema}.{table} not built yet")
    return {(r[0], r[1]) for r in rows}


@pytest.mark.parametrize("table,pg_schema,trino_schema", [
    ("agg_country_traffic", "marts", "gold"),
    ("agg_hourly_traffic", "marts", "gold"),
    ("anomalies", "marts", "gold"),
    ("fact_state_snapshots", "marts", "silver"),
])
def test_column_types_match(table, pg_schema, trino_schema):
    pg = _simplify(_pg_columns(table, pg_schema))
    tr = _simplify(_trino_columns(table, trino_schema))
    assert pg == tr, (
        f"column-type drift on {table}: pg_only={pg - tr} trino_only={tr - pg}"
    )

    # The v2.7 hard gate: main_dttm_col must be tz-aware on the Trino side.
    dttm = MAIN_DTTM_COL[table]
    tr_types = dict(tr)
    assert tr_types.get(dttm) == "timestamp with time zone", (
        f"{table}.{dttm} must be TIMESTAMP WITH TIME ZONE on Trino, "
        f"got {tr_types.get(dttm)!r} — would break Superset v2.7 cutover"
    )


def test_fact_state_snapshots_count_matches_for_yesterday():
    # An exactly-completed UTC day is stable, so this is an exact-match check.
    day_filter = "snapshot_time >= date_trunc('day', current_timestamp) - interval '1' day " \
                 "and snapshot_time < date_trunc('day', current_timestamp)"
    pg = _pg_query(
        "SELECT count(*) FROM marts.fact_state_snapshots WHERE "
        "snapshot_time >= date_trunc('day', now()) - interval '1 day' "
        "AND snapshot_time < date_trunc('day', now())"
    )[0][0]
    tr = _trino_query(
        f"SELECT count(*) FROM iceberg.silver.fact_state_snapshots WHERE {day_filter}"
    )[0][0]
    assert pg == tr, f"yesterday row count drift: pg={pg} trino={tr}"


def test_anomalies_count_matches_within_24h():
    # End the window one cadence back so both independently-timed dbt builds
    # compare settled data (postgres reads staging.raw_states, trino reads live
    # bronze — the edge moves between the two parallel runs).
    pg = _pg_query(
        "SELECT count(*) FROM marts.anomalies "
        "WHERE snapshot_time >= now() - interval '24 hours' "
        "AND snapshot_time < now() - interval '15 minutes'"
    )[0][0]
    tr = _trino_query(
        "SELECT count(*) FROM iceberg.gold.anomalies "
        "WHERE snapshot_time >= current_timestamp - interval '24' hour "
        "AND snapshot_time < current_timestamp - interval '15' minute"
    )[0][0]
    if pg == 0 and tr == 0:
        return
    denom = max(pg, tr, 1)
    assert abs(pg - tr) / denom <= 0.01, f"anomalies 24h count drift >1%: pg={pg} trino={tr}"


def test_agg_hourly_traffic_unique_aircraft_match():
    # Compare the most recent *complete* hour so both targets see settled data.
    pg = _pg_query(
        "SELECT unique_aircraft FROM marts.agg_hourly_traffic "
        "WHERE snapshot_hour = date_trunc('hour', now()) - interval '1 hour'"
    )
    tr = _trino_query(
        "SELECT unique_aircraft FROM iceberg.gold.agg_hourly_traffic "
        "WHERE snapshot_hour = date_trunc('hour', current_timestamp) - interval '1' hour"
    )
    if not pg or not tr:
        pytest.skip("no completed-hour row on one side yet")
    assert pg[0][0] == tr[0][0], (
        f"unique_aircraft drift for last complete hour: pg={pg[0][0]} trino={tr[0][0]}"
    )


def test_agg_country_traffic_row_counts_match():
    # Latest-snapshot mart is live; allow small skew between the parallel dbt runs.
    pg = _pg_query("SELECT count(*) FROM marts.agg_country_traffic")[0][0]
    tr = _trino_query("SELECT count(*) FROM iceberg.gold.agg_country_traffic")[0][0]
    if pg == 0 and tr == 0:
        pytest.skip("agg_country_traffic empty on both sides")
    denom = max(pg, tr, 1)
    assert abs(pg - tr) / denom <= 0.05, (
        f"agg_country_traffic country count drift >5%: pg={pg} trino={tr}"
    )
