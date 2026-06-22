"""Shared fixtures for DAG and pipeline tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from airflow.models import DagBag

from include import adsb_manifest as am


REPO_ROOT = Path(__file__).resolve().parent.parent
DAGS_FOLDER = REPO_ROOT / "dags"

# Schema-less sqlite mirror of public.adsb_ingestion_manifest (same convention as test_manifest).
_SQLITE_DDL = """
CREATE TABLE adsb_ingestion_manifest (
    filename                TEXT PRIMARY KEY,
    process_uuid            TEXT,
    stream                  TEXT,
    hostname                TEXT,
    rotation_start_ts       TEXT,
    rotation_end_ts         TEXT,
    complete                BOOLEAN,
    row_count               INTEGER,
    frame_count             INTEGER,
    byte_count              INTEGER,
    beast_uncompressed_size INTEGER,
    schema_version          INTEGER,
    s3_uri                  TEXT,
    manifest_s3_uri         TEXT,
    landed_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    iceberg_committed_at    TIMESTAMP,
    iceberg_snapshot_id     INTEGER,
    ch_loaded_at            TIMESTAMP,
    provenance              TEXT DEFAULT 'live'
)
"""


@pytest.fixture(scope="session")
def dagbag() -> DagBag:
    """Parse the project's DAGs once per test session."""
    return DagBag(dag_folder=str(DAGS_FOLDER), include_examples=False)


@pytest.fixture(scope="session")
def ch_cur():
    # Live ClickHouse cursor-shim for the serving-mart integration tests: skips when CH is unreachable
    # (host / CI without the stack), runs for real inside the airflow containers; missing tables fail RED.
    try:
        import clickhouse_connect
    except ImportError as exc:
        pytest.skip(f"clickhouse-connect not available: {exc}")
    try:
        client = clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
            settings={"join_use_nulls": 1},
        )
        client.query("SELECT 1")
    except clickhouse_connect.driver.exceptions.OperationalError as exc:
        # Only network unreachability skips; config/auth/programming errors fail loudly (RED).
        pytest.skip(f"clickhouse not reachable: {exc}")

    class _Cur:
        # Minimal DBAPI-ish shim so the mart tests can keep `cur.execute(sql); cur.fetchall()`.
        def __init__(self, c):
            self._c = c
            self._rows: list = []

        def execute(self, sql, params=None):
            self._rows = self._c.query(sql, parameters=params or {}).result_rows

        def fetchall(self):
            return self._rows

    try:
        yield _Cur(client)
    finally:
        client.close()


@pytest.fixture
def adsb_manifest_eng(monkeypatch):
    monkeypatch.setattr(am, "_TABLE", "adsb_ingestion_manifest")
    e = sa.create_engine("sqlite:///:memory:")
    with e.begin() as conn:
        conn.execute(sa.text(_SQLITE_DDL))
    return e
