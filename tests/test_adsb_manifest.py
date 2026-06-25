from __future__ import annotations

import pytest
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


def _adsb_loaded(eng, filename, *, ch_loaded_at=None, archived_at=None):
    # Far-past / far-future ch_loaded_at strings keep the age comparison wall-clock- and
    # bind-format-independent (the year dominates lexicographically) in the sqlite mirror.
    _adsb(eng, filename)
    with eng.begin() as c:
        c.execute(
            sa.text("UPDATE adsb_ingestion_manifest SET ch_loaded_at=:ch, archived_at=:arc WHERE filename=:f"),
            {"ch": ch_loaded_at, "arc": archived_at, "f": filename},
        )


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


def test_pending_ch_adsb_uris_independent_of_iceberg_marker(adsb_manifest_eng):
    # CH marker advances separately from the Iceberg marker (P2 non-blocking invariant).
    _adsb(adsb_manifest_eng, "a.parquet")
    _adsb(adsb_manifest_eng, "b.parquet")
    am.mark_iceberg_committed({"a.parquet": 111}, engine=adsb_manifest_eng)

    iceberg_pending = [p["filename"] for p in am.pending_adsb_uris(engine=adsb_manifest_eng)]
    ch_pending = [p["filename"] for p in am.pending_ch_adsb_uris(engine=adsb_manifest_eng)]
    assert iceberg_pending == ["b.parquet"]            # 'a' committed to Iceberg
    assert sorted(ch_pending) == ["a.parquet", "b.parquet"]  # both still CH-pending
    assert ch_pending and all("s3_uri" in p for p in am.pending_ch_adsb_uris(engine=adsb_manifest_eng))


def test_mark_ch_loaded_idempotent_and_excludes_committed(adsb_manifest_eng):
    _adsb(adsb_manifest_eng, "a.parquet")
    _adsb(adsb_manifest_eng, "b.parquet")
    n1 = am.mark_ch_loaded(["a.parquet"], engine=adsb_manifest_eng)
    n2 = am.mark_ch_loaded(["a.parquet"], engine=adsb_manifest_eng)
    assert n1 == 1
    assert n2 == 0
    assert [p["filename"] for p in am.pending_ch_adsb_uris(engine=adsb_manifest_eng)] == ["b.parquet"]
    # CH marker must not have advanced the Iceberg marker.
    assert len(am.pending_adsb_uris(engine=adsb_manifest_eng)) == 2


def test_mark_ch_loaded_empty_is_noop(adsb_manifest_eng):
    assert am.mark_ch_loaded([], engine=adsb_manifest_eng) == 0


def test_pending_archive_adsb_uris_selects_aged_loaded_unarchived(adsb_manifest_eng):
    _adsb_loaded(adsb_manifest_eng, "aged.parquet", ch_loaded_at="2020-01-01 00:00:00")
    _adsb_loaded(adsb_manifest_eng, "recent.parquet", ch_loaded_at="2099-01-01 00:00:00")
    _adsb_loaded(adsb_manifest_eng, "never.parquet", ch_loaded_at=None)
    _adsb_loaded(adsb_manifest_eng, "done.parquet",
                 ch_loaded_at="2020-01-01 00:00:00", archived_at="2021-01-01 00:00:00")

    rows = am.pending_archive_adsb_uris(older_than_days=14, engine=adsb_manifest_eng)
    assert [p["filename"] for p in rows] == ["aged.parquet"]
    assert rows[0]["s3_uri"].endswith("aged.parquet")


def test_mark_archived_adsb_is_idempotent_and_clears_pending(adsb_manifest_eng):
    _adsb_loaded(adsb_manifest_eng, "a.parquet", ch_loaded_at="2020-01-01 00:00:00")
    n1 = am.mark_archived(["a.parquet"], engine=adsb_manifest_eng)
    n2 = am.mark_archived(["a.parquet"], engine=adsb_manifest_eng)
    assert n1 == 1
    assert n2 == 0
    assert am.pending_archive_adsb_uris(older_than_days=14, engine=adsb_manifest_eng) == []


def test_mark_archived_adsb_empty_is_noop(adsb_manifest_eng):
    assert am.mark_archived([], engine=adsb_manifest_eng) == 0


def test_pending_archive_adsb_uris_respects_limit(adsb_manifest_eng):
    for i in range(3):
        _adsb_loaded(adsb_manifest_eng, f"f{i}.parquet", ch_loaded_at="2020-01-01 00:00:00")
    assert len(am.pending_archive_adsb_uris(older_than_days=14, engine=adsb_manifest_eng, limit=2)) == 2


def test_pending_archive_adsb_uris_rejects_negative_limit(adsb_manifest_eng):
    with pytest.raises(ValueError, match="limit must not be negative"):
        am.pending_archive_adsb_uris(older_than_days=14, engine=adsb_manifest_eng, limit=-1)


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
