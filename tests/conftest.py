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
    provenance              TEXT DEFAULT 'live'
)
"""


@pytest.fixture(scope="session")
def dagbag() -> DagBag:
    """Parse the project's DAGs once per test session."""
    return DagBag(dag_folder=str(DAGS_FOLDER), include_examples=False)


@pytest.fixture(scope="session")
def cur():
    try:
        import trino
    except ImportError as exc:
        pytest.skip(f"trino client not available: {exc}")
    try:
        conn = trino.dbapi.connect(
            host=os.environ.get("TRINO_HOST", "trino-coordinator"),
            port=int(os.environ.get("TRINO_PORT", "8080")),
            user="root", catalog="iceberg", http_scheme="http",
        )
        c = conn.cursor()
        c.execute("SELECT 1")
        c.fetchall()
    except Exception as exc:  # only infra unreachability skips; missing tables must fail loudly (RED)
        pytest.skip(f"trino not reachable: {exc}")
    try:
        yield c
    finally:
        c.close()
        conn.close()


@pytest.fixture
def adsb_manifest_eng(monkeypatch):
    monkeypatch.setattr(am, "_TABLE", "adsb_ingestion_manifest")
    e = sa.create_engine("sqlite:///:memory:")
    with e.begin() as conn:
        conn.execute(sa.text(_SQLITE_DDL))
    return e


@pytest.fixture
def local_catalog(tmp_path):
    # Hermetic on-disk catalog; in-fixture import keeps pyiceberg off unrelated tests' collection path.
    from pyiceberg.catalog.sql import SqlCatalog

    warehouse = tmp_path / "wh"
    warehouse.mkdir()
    return SqlCatalog(
        "test",
        uri=f"sqlite:///{tmp_path / 'catalog.db'}",
        warehouse=f"file://{warehouse}",
    )
