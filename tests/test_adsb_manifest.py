from __future__ import annotations

import sqlalchemy as sa

from include import adsb_manifest as am


def _adsb(eng, filename, **over):
    kw = dict(
        filename=filename, process_uuid="5f3b0bb5-7da1-48d5-be0c-9cff1808a86f",
        stream="adsb_state", hostname="sangenjaya-edge",
        rotation_start_ts="2026-05-29T00:00:00Z", rotation_end_ts="2026-05-29T01:00:00Z",
        complete=True, schema_version=1, row_count=45800,
        s3_uri=f"s3://sancha1090/bronze/adsb_state/dt=2026-05-29/{filename}",
        manifest_s3_uri=f"s3://sancha1090/bronze/adsb_state/dt=2026-05-29/{filename}.manifest.json",
    )
    kw.update(over)
    am.record_bundle(engine=eng, **kw)


def test_record_bundle_idempotent_on_filename(adsb_manifest_eng):
    _adsb(adsb_manifest_eng, "f1.parquet", row_count=10)
    _adsb(adsb_manifest_eng, "f1.parquet", row_count=999)  # conflict → ignored
    with adsb_manifest_eng.begin() as c:
        n = c.execute(sa.text("SELECT count(*) FROM adsb_ingestion_manifest")).scalar()
        rc = c.execute(sa.text("SELECT row_count FROM adsb_ingestion_manifest WHERE filename='f1.parquet'")).scalar()
    assert n == 1
    assert rc == 10


def test_record_bundle_handles_both_streams(adsb_manifest_eng):
    _adsb(adsb_manifest_eng, "state.parquet", stream="adsb_state", row_count=100)
    am.record_bundle(
        engine=adsb_manifest_eng, filename="beast.beast.gz",
        process_uuid="5f3b0bb5-7da1-48d5-be0c-9cff1808a86f",
        stream="beast_raw", hostname="sangenjaya-edge",
        rotation_start_ts="2026-05-29T00:00:00Z", rotation_end_ts="2026-05-29T01:00:00Z",
        complete=True, schema_version=1,
        frame_count=423491, byte_count=4265615, beast_uncompressed_size=7818463,
        s3_uri="s3://sancha1090/bronze/beast_raw/dt=2026-05-29/beast.beast.gz",
        manifest_s3_uri="s3://sancha1090/bronze/beast_raw/dt=2026-05-29/beast.manifest.json",
    )
    with adsb_manifest_eng.begin() as c:
        rows = {r[0]: r[1] for r in c.execute(sa.text(
            "SELECT stream, row_count FROM adsb_ingestion_manifest ORDER BY stream"))}
        beast_frames = c.execute(sa.text(
            "SELECT frame_count FROM adsb_ingestion_manifest WHERE stream='beast_raw'")).scalar()
    assert rows["adsb_state"] == 100
    assert rows["beast_raw"] is None       # beast has no row_count
    assert beast_frames == 423491


def test_already_ingested_returns_existing_subset(adsb_manifest_eng):
    _adsb(adsb_manifest_eng, "a.parquet")
    _adsb(adsb_manifest_eng, "b.parquet")
    got = am.already_ingested(["a.parquet", "b.parquet", "c.parquet"], engine=adsb_manifest_eng)
    assert got == {"a.parquet", "b.parquet"}


def test_already_ingested_empty_input(adsb_manifest_eng):
    assert am.already_ingested([], engine=adsb_manifest_eng) == set()


def test_pending_adsb_uris_excludes_beast_and_committed(adsb_manifest_eng):
    _adsb(adsb_manifest_eng, "pending.parquet", row_count=1)
    _adsb(adsb_manifest_eng, "done.parquet", row_count=2)
    am.record_bundle(
        engine=adsb_manifest_eng, filename="b.beast.gz",
        process_uuid="5f3b0bb5-7da1-48d5-be0c-9cff1808a86f",
        stream="beast_raw", hostname="h", rotation_start_ts="2026-05-29T00:00:00Z",
        rotation_end_ts="2026-05-29T01:00:00Z", complete=True, schema_version=1,
        frame_count=1, byte_count=1, beast_uncompressed_size=1,
        s3_uri="s3://b/x", manifest_s3_uri="s3://b/x.json",
    )
    am.mark_iceberg_committed({"done.parquet": 123}, engine=adsb_manifest_eng)

    pending = am.pending_adsb_uris(engine=adsb_manifest_eng)
    names = [p["filename"] for p in pending]
    assert names == ["pending.parquet"]              # beast + committed excluded
    assert pending[0]["s3_uri"].endswith("pending.parquet")


def test_newest_adsb_rotation_end_returns_max_ignoring_beast(adsb_manifest_eng):
    from datetime import datetime, timezone

    _adsb(adsb_manifest_eng, "early.parquet", rotation_end_ts="2026-05-29T01:00:00Z")
    _adsb(adsb_manifest_eng, "late.parquet", rotation_end_ts="2026-05-29T05:30:00Z")
    am.record_bundle(  # a later beast close time must NOT count (adsb_state only)
        engine=adsb_manifest_eng, filename="b.beast.gz",
        process_uuid="5f3b0bb5-7da1-48d5-be0c-9cff1808a86f",
        stream="beast_raw", hostname="h", rotation_start_ts="2026-05-29T00:00:00Z",
        rotation_end_ts="2026-05-29T09:00:00Z", complete=True, schema_version=1,
        frame_count=1, byte_count=1, beast_uncompressed_size=1,
        s3_uri="s3://b/x", manifest_s3_uri="s3://b/x.json",
    )
    assert am.newest_adsb_rotation_end(engine=adsb_manifest_eng) == datetime(
        2026, 5, 29, 5, 30, tzinfo=timezone.utc)


def test_newest_adsb_rotation_end_none_when_empty(adsb_manifest_eng):
    assert am.newest_adsb_rotation_end(engine=adsb_manifest_eng) is None


def test_mark_iceberg_committed_sets_snapshot_per_file_and_is_idempotent(adsb_manifest_eng):
    _adsb(adsb_manifest_eng, "x.parquet")
    _adsb(adsb_manifest_eng, "y.parquet")
    n1 = am.mark_iceberg_committed({"x.parquet": 111, "y.parquet": 222}, engine=adsb_manifest_eng)
    n2 = am.mark_iceberg_committed(  # already set
        {"x.parquet": 111, "y.parquet": 222}, engine=adsb_manifest_eng)
    with adsb_manifest_eng.begin() as c:
        sid = c.execute(sa.text(
            "SELECT iceberg_snapshot_id FROM adsb_ingestion_manifest WHERE filename='y.parquet'")).scalar()
    assert n1 == 2
    assert n2 == 0          # nothing left to commit
    assert sid == 222
