from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from include import adsb_iceberg as ai
from include import adsb_manifest as am
from dags import tableize_adsb as ta


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "adsb" / "sample_adsb_state.parquet"

_SQLITE_DDL = """
CREATE TABLE adsb_ingestion_manifest (
    filename TEXT PRIMARY KEY, process_uuid TEXT, stream TEXT, hostname TEXT,
    rotation_start_ts TEXT, rotation_end_ts TEXT, complete BOOLEAN,
    row_count INTEGER, frame_count INTEGER, byte_count INTEGER, beast_uncompressed_size INTEGER,
    schema_version INTEGER, s3_uri TEXT, manifest_s3_uri TEXT,
    landed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, iceberg_committed_at TIMESTAMP,
    iceberg_snapshot_id INTEGER, provenance TEXT DEFAULT 'live'
)
"""


@pytest.fixture
def eng(monkeypatch):
    monkeypatch.setattr(am, "_TABLE", "adsb_ingestion_manifest")
    e = sa.create_engine("sqlite:///:memory:")
    with e.begin() as conn:
        conn.execute(sa.text(_SQLITE_DDL))
    return e


@pytest.fixture
def catalog(tmp_path):
    from pyiceberg.catalog.sql import SqlCatalog

    wh = tmp_path / "wh"
    wh.mkdir()
    return SqlCatalog("test", uri=f"sqlite:///{tmp_path / 'cat.db'}", warehouse=f"file://{wh}")


def _seed_pending(eng, filename, s3_uri):
    am.record_bundle(
        engine=eng, filename=filename, process_uuid="5f3b0bb5-7da1-48d5-be0c-9cff1808a86f",
        stream="adsb_state", hostname="sangenjaya-edge",
        rotation_start_ts="2026-05-29T00:00:00Z", rotation_end_ts="2026-05-29T01:00:00Z",
        complete=True, schema_version=1, row_count=5, s3_uri=s3_uri,
        manifest_s3_uri=s3_uri + ".manifest.json",
    )


def test_tableize_core_short_circuits_when_nothing_pending(eng, catalog):
    result = ta.tableize_core(catalog, eng)
    assert result["committed"] == 0
    table = catalog.load_table(ai.QUALIFIED)
    assert table.current_snapshot() is None  # ensured but never appended


def test_tableize_core_commits_pending_and_marks_manifest(eng, catalog):
    _seed_pending(eng, "h_adsb_state_2026-05-29T00_5f3b.parquet", str(FIXTURE))

    result = ta.tableize_core(catalog, eng)

    assert result["committed"] == 1
    table = catalog.load_table(ai.QUALIFIED)
    assert table.scan().to_arrow().num_rows == 5
    with eng.begin() as c:
        committed_at, sid = c.execute(sa.text(
            "SELECT iceberg_committed_at, iceberg_snapshot_id FROM adsb_ingestion_manifest "
            "WHERE filename='h_adsb_state_2026-05-29T00_5f3b.parquet'")).fetchone()
    assert committed_at is not None
    assert sid == table.current_snapshot().snapshot_id


def test_tableize_core_idempotent_replay_after_partial_commit(eng, catalog):
    fname = "h_adsb_state_2026-05-29T00_5f3b.parquet"
    _seed_pending(eng, fname, str(FIXTURE))
    ta.tableize_core(catalog, eng)

    # Simulate add_files committed but mark_committed crashed: clear the manifest flags, replay.
    with eng.begin() as c:
        c.execute(sa.text("UPDATE adsb_ingestion_manifest SET iceberg_committed_at=NULL, "
                          "iceberg_snapshot_id=NULL WHERE filename=:f"), {"f": fname})

    result = ta.tableize_core(catalog, eng)

    assert result["committed"] == 1  # reconciled, not re-added
    table = catalog.load_table(ai.QUALIFIED)
    assert table.scan().to_arrow().num_rows == 5  # not 10
