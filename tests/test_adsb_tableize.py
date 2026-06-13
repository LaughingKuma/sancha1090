from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from include import adsb_iceberg as ai
from include import adsb_manifest as am
from dags import tableize_adsb as ta


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "adsb" / "sample_adsb_state.parquet"


def _seed_pending(eng, filename, s3_uri):
    am.record_bundle(
        engine=eng, filename=filename, process_uuid="5f3b0bb5-7da1-48d5-be0c-9cff1808a86f",
        stream="adsb_state", hostname="sangenjaya-edge",
        rotation_start_ts="2026-05-29T00:00:00Z", rotation_end_ts="2026-05-29T01:00:00Z",
        complete=True, schema_version=1, row_count=5, s3_uri=s3_uri,
        manifest_s3_uri=s3_uri + ".manifest.json",
    )


def test_tableize_core_short_circuits_when_nothing_pending(adsb_manifest_eng, local_catalog):
    result = ta.tableize_core(local_catalog, adsb_manifest_eng)
    assert result["committed"] == 0
    table = local_catalog.load_table(ai.QUALIFIED)
    assert table.current_snapshot() is None  # ensured but never appended


def test_tableize_core_commits_pending_and_marks_manifest(adsb_manifest_eng, local_catalog):
    _seed_pending(adsb_manifest_eng, "h_adsb_state_2026-05-29T00_5f3b.parquet", str(FIXTURE))

    result = ta.tableize_core(local_catalog, adsb_manifest_eng)

    assert result["committed"] == 1
    table = local_catalog.load_table(ai.QUALIFIED)
    assert table.scan().to_arrow().num_rows == 5
    with adsb_manifest_eng.begin() as c:
        committed_at, sid = c.execute(sa.text(
            "SELECT iceberg_committed_at, iceberg_snapshot_id FROM adsb_ingestion_manifest "
            "WHERE filename='h_adsb_state_2026-05-29T00_5f3b.parquet'")).fetchone()
    assert committed_at is not None
    assert sid == table.current_snapshot().snapshot_id


def test_tableize_core_idempotent_replay_after_partial_commit(adsb_manifest_eng, local_catalog):
    fname = "h_adsb_state_2026-05-29T00_5f3b.parquet"
    _seed_pending(adsb_manifest_eng, fname, str(FIXTURE))
    ta.tableize_core(local_catalog, adsb_manifest_eng)

    # Simulate add_files committed but mark_committed crashed: clear the manifest flags, replay.
    with adsb_manifest_eng.begin() as c:
        c.execute(sa.text("UPDATE adsb_ingestion_manifest SET iceberg_committed_at=NULL, "
                          "iceberg_snapshot_id=NULL WHERE filename=:f"), {"f": fname})

    result = ta.tableize_core(local_catalog, adsb_manifest_eng)

    assert result["committed"] == 1  # reconciled, not re-added
    table = local_catalog.load_table(ai.QUALIFIED)
    assert table.scan().to_arrow().num_rows == 5  # not 10
