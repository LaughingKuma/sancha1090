from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from pyarrow.fs import LocalFileSystem

from include import adsb_iceberg as ai
from include import adsb_manifest as am
from dags import backfill_adsb as bd


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


def _lines():
    return [
        json.dumps({"capture_ts": 1779532053.0, "msg": {"hex": "a", "flight": "AAA111  "}}),  # 05-23T10
        json.dumps({"capture_ts": 1779532100.0, "msg": {"hex": "b"}}),                          # 05-23T10
        json.dumps({"capture_ts": 1779535660.0, "msg": {"hex": "c"}}),                          # 05-23T11
        json.dumps({"capture_ts": 1779937200.0, "msg": {"hex": "z"}}),                          # 05-28T03 (live → skip)
    ]


def test_backfill_core_writes_skips_overlap_and_registers(tmp_path, eng, catalog):
    result = bd.backfill_core(
        lines=_lines(), catalog=catalog, engine=eng, fs=LocalFileSystem(),
        bucket=str(tmp_path), end_before_hour="2026-05-28T03", uri_prefix="",
    )
    assert result == {"hours": 2, "rows": 3, "committed": 2}

    table = catalog.load_table(ai.QUALIFIED)
    assert table.scan().to_arrow().num_rows == 3  # the 05-28T03 record was skipped, not loaded

    with eng.begin() as c:
        rows = c.execute(sa.text(
            "SELECT provenance, iceberg_committed_at IS NOT NULL FROM adsb_ingestion_manifest")).fetchall()
    assert len(rows) == 2
    assert all(prov == "backfill" and committed for prov, committed in rows)


def test_backfill_core_is_idempotent_on_replay(tmp_path, eng, catalog):
    kw = dict(catalog=catalog, engine=eng, fs=LocalFileSystem(),
              bucket=str(tmp_path), end_before_hour="2026-05-28T03", uri_prefix="")
    bd.backfill_core(lines=_lines(), **kw)
    # Re-run the same batch (e.g. a retried trigger): files already present → reconciled, not doubled.
    result = bd.backfill_core(lines=_lines(), **kw)
    assert result["committed"] == 0               # already committed on the first run → nothing new
    table = catalog.load_table(ai.QUALIFIED)
    assert table.scan().to_arrow().num_rows == 3  # not 6 — add_files reconciled, no double-add


def test_backfill_beast_core_copies_and_records_manifest(tmp_path, eng):
    src = tmp_path / "src"
    src.mkdir()
    (src / "legacy.beast.gz").write_bytes(b"BEASTBYTES" * 100)
    (src / "legacy.beastidx.gz").write_bytes(b"IDX")

    result = bd.backfill_beast_core(
        engine=eng, fs=LocalFileSystem(), bucket=str(tmp_path),
        source_beast_key=str(src / "legacy.beast.gz"),
        source_idx_key=str(src / "legacy.beastidx.gz"),
        day="2026-05-23", uri_prefix="",
    )
    assert result["byte_count"] == 1000
    # both objects copied into beast_raw/dt=.../
    assert (tmp_path / "bronze" / "beast_raw" / "dt=2026-05-23").exists()
    with eng.begin() as c:
        stream, prov, bc, rc = c.execute(sa.text(
            "SELECT stream, provenance, byte_count, row_count FROM adsb_ingestion_manifest "
            "WHERE stream='beast_raw'")).fetchone()
    assert (stream, prov, bc) == ("beast_raw", "backfill", 1000)
    assert rc is None                              # beast carries no row_count
