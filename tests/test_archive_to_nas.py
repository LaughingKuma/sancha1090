from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import sqlalchemy as sa
from pyarrow.fs import LocalFileSystem

from include import adsb_manifest as am
from include import archive_to_nas as arc
from include import manifest


# Mirror garage_pyarrow_fs() with a local filesystem so the copy/verify path runs with no S3 or NFS.
_FS = LocalFileSystem()

_INGEST_DDL = (
    "CREATE TABLE ingestion_manifest ("
    " object_uri TEXT PRIMARY KEY, loaded_at TIMESTAMP, snapshot_min INTEGER,"
    " snapshot_max INTEGER, row_count INTEGER,"
    " ch_loaded_at TIMESTAMP, archived_at TIMESTAMP)"
)


@pytest.fixture(autouse=True)
def _clear_archive_env(monkeypatch):
    # Hermetic: in-container test runs must not inherit the prod scheduler's ARCHIVE_* env (esp.
    # ARCHIVE_REQUIRE_MOUNT=1, which would red every tmp-dir test) — each test passes what it needs explicitly.
    for var in ("ARCHIVE_REQUIRE_MOUNT", "ARCHIVE_COLD_PATH", "ARCHIVE_OLDER_THAN_DAYS", "ARCHIVE_MAX_FILES"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def ingest_eng(monkeypatch):
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    eng = sa.create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(sa.text(_INGEST_DDL))
    return eng


def _write_parquet(path: Path, n: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"x": list(range(n)), "y": [f"r{i}" for i in range(n)]}), str(path))


def _src_uri(src_root: Path, rel: str) -> str:
    # Absolute local path under an s3:// prefix so _read_key() strips back to the real local path that
    # LocalFileSystem can open, exactly as garage_pyarrow_fs() strips the s3:// for a Garage key.
    return f"s3://{src_root / rel}"


def _insert_ingest(eng, uri, *, ch_loaded_at="2020-01-01 00:00:00", archived_at=None, rows=5):
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ingestion_manifest (object_uri, row_count, ch_loaded_at, archived_at)"
                " VALUES (:u, :r, :c, :a)"
            ),
            {"u": uri, "r": rows, "c": ch_loaded_at, "a": archived_at},
        )


def _insert_adsb(eng, filename, src_root, rel, *, rows=5, ch_loaded_at="2020-01-01 00:00:00", archived_at=None):
    am.record_bundle(
        engine=eng, filename=filename, process_uuid="5f3b0bb5-7da1-48d5-be0c-9cff1808a86f",
        stream="adsb_state", hostname="edge", rotation_start_ts="2026-05-29T00:00:00Z",
        rotation_end_ts="2026-05-29T01:00:00Z", complete=True, schema_version=1, row_count=rows,
        s3_uri=_src_uri(src_root, rel), manifest_s3_uri=_src_uri(src_root, rel + ".manifest.json"),
    )
    with eng.begin() as c:
        c.execute(
            sa.text("UPDATE adsb_ingestion_manifest SET ch_loaded_at=:ch, archived_at=:a WHERE filename=:f"),
            {"ch": ch_loaded_at, "a": archived_at, "f": filename},
        )


def _archived(eng, table, key_col, key) -> bool:
    with eng.begin() as c:
        v = c.execute(
            sa.text(f"SELECT archived_at FROM {table} WHERE {key_col}=:k"), {"k": key}
        ).scalar()
    return v is not None


def test_archive_pending_copies_verifies_and_flips(tmp_path, ingest_eng, adsb_manifest_eng):
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    states_rel = "bronze/states_raw/dt=2026-06-01/s.parquet"
    adsb_rel = "bronze/adsb_state/dt=2026-06-01/a.parquet"
    _write_parquet(src / states_rel, n=5)
    _write_parquet(src / adsb_rel, n=7)
    _insert_ingest(ingest_eng, _src_uri(src, states_rel))
    _insert_adsb(adsb_manifest_eng, "a.parquet", src, adsb_rel, rows=7)

    out = arc.archive_pending(
        fs=_FS, cold_path=cold, older_than_days=14, engine=ingest_eng, adsb_engine=adsb_manifest_eng
    )

    assert out["ok"] is True
    assert out["files"] == 2
    assert out["rows"] == 12  # 5 + 7 verified via parquet metadata.num_rows
    assert (cold / states_rel).read_bytes() == (src / states_rel).read_bytes()
    assert (cold / adsb_rel).read_bytes() == (src / adsb_rel).read_bytes()
    assert _archived(ingest_eng, "ingestion_manifest", "object_uri", _src_uri(src, states_rel))
    assert _archived(adsb_manifest_eng, "adsb_ingestion_manifest", "filename", "a.parquet")
    # GC is off, so the Garage source must stay (it's CH's s3() source).
    assert (src / states_rel).exists()
    assert (src / adsb_rel).exists()


def test_archive_pending_skips_recent_and_already_archived(tmp_path, ingest_eng, adsb_manifest_eng):
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    aged = "bronze/states_raw/dt=2026-06-01/aged.parquet"
    recent = "bronze/states_raw/dt=2026-06-20/recent.parquet"
    done = "bronze/states_raw/dt=2026-05-01/done.parquet"
    for rel in (aged, recent, done):
        _write_parquet(src / rel)
    _insert_ingest(ingest_eng, _src_uri(src, aged), ch_loaded_at="2020-01-01 00:00:00")
    _insert_ingest(ingest_eng, _src_uri(src, recent), ch_loaded_at="2099-01-01 00:00:00")
    _insert_ingest(ingest_eng, _src_uri(src, done),
                   ch_loaded_at="2020-01-01 00:00:00", archived_at="2021-01-01 00:00:00")

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)

    assert out["files"] == 1
    assert (cold / aged).exists()
    assert not (cold / recent).exists()
    assert not (cold / done).exists()


def test_archive_pending_is_idempotent(tmp_path, ingest_eng, adsb_manifest_eng):
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    rel = "bronze/flights_raw/dt=2026-06-01/f.parquet"
    _write_parquet(src / rel)
    _insert_ingest(ingest_eng, _src_uri(src, rel))

    first = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                                engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    second = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                                 engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert first["files"] == 1
    assert second["files"] == 0  # already archived -> no re-copy
    assert second["ok"] is True


def test_archive_pending_recovers_dest_present_but_unmarked(tmp_path, ingest_eng, adsb_manifest_eng):
    # Crash between copy and the archived_at flip: dest already matches source, archived_at still NULL.
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    rel = "bronze/states_raw/dt=2026-06-01/s.parquet"
    _write_parquet(src / rel)
    _insert_ingest(ingest_eng, _src_uri(src, rel))
    (cold / rel).parent.mkdir(parents=True, exist_ok=True)
    (cold / rel).write_bytes((src / rel).read_bytes())  # the prior run's verified copy, before it could mark

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["ok"] is True
    assert out["files"] == 1
    assert _archived(ingest_eng, "ingestion_manifest", "object_uri", _src_uri(src, rel))


def test_archive_pending_skips_when_cold_path_absent(tmp_path, ingest_eng, adsb_manifest_eng):
    src = tmp_path / "garage"
    rel = "bronze/states_raw/dt=2026-06-01/s.parquet"
    _write_parquet(src / rel)
    _insert_ingest(ingest_eng, _src_uri(src, rel))

    out = arc.archive_pending(fs=_FS, cold_path=tmp_path / "no-such-mount", older_than_days=14,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["ok"] is True
    assert out["files"] == 0
    assert out.get("skipped")
    assert not _archived(ingest_eng, "ingestion_manifest", "object_uri", _src_uri(src, rel))


def test_archive_pending_required_mount_raises_when_unmounted(tmp_path, ingest_eng, adsb_manifest_eng, monkeypatch):
    # ARCHIVE_REQUIRE_MOUNT (set on the production scheduler) turns an absent/non-mountpoint cold path into a loud
    # failure rather than a green skip — a regressed NFS mount must not silently disable archival.
    monkeypatch.setenv("ARCHIVE_REQUIRE_MOUNT", "1")
    cold = tmp_path / "cold"
    cold.mkdir()  # exists but is NOT a mountpoint
    with pytest.raises(RuntimeError):
        arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                            engine=ingest_eng, adsb_engine=adsb_manifest_eng)


def test_archive_pending_required_mount_proceeds_when_mounted(tmp_path, ingest_eng, adsb_manifest_eng, monkeypatch):
    monkeypatch.setenv("ARCHIVE_REQUIRE_MOUNT", "1")
    monkeypatch.setattr("os.path.ismount", lambda _p: True)  # simulate the NFS mountpoint
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    rel = "bronze/states_raw/dt=2026-06-01/s.parquet"
    _write_parquet(src / rel)
    _insert_ingest(ingest_eng, _src_uri(src, rel))

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["files"] == 1


def test_archive_pending_unreadable_source_reds_and_leaves_pending(tmp_path, ingest_eng, adsb_manifest_eng):
    # A non-parquet/corrupt source fails the rowcount probe -> the file is skipped (ok=False) and stays
    # pending (archived_at NULL) so the next run retries; it is never marked-without-a-verified-copy.
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    rel = "bronze/states_raw/dt=2026-06-01/corrupt.parquet"
    (src / rel).parent.mkdir(parents=True, exist_ok=True)
    (src / rel).write_bytes(b"not a parquet file")
    _insert_ingest(ingest_eng, _src_uri(src, rel))

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["ok"] is False
    assert out["files"] == 0
    assert not _archived(ingest_eng, "ingestion_manifest", "object_uri", _src_uri(src, rel))


def test_archive_pending_rejects_rowcount_drift(tmp_path, ingest_eng, adsb_manifest_eng):
    # The Garage source must still match what CH loaded: a parquet whose rowcount differs from the manifest's
    # recorded row_count (a replaced/corrupt object) is refused — reds and stays pending, never archived.
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    rel = "bronze/states_raw/dt=2026-06-01/drift.parquet"
    _write_parquet(src / rel, n=7)  # source now has 7 rows
    _insert_ingest(ingest_eng, _src_uri(src, rel), rows=5)  # but CH loaded 5

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["ok"] is False
    assert out["files"] == 0
    assert not (cold / rel).exists()
    assert not _archived(ingest_eng, "ingestion_manifest", "object_uri", _src_uri(src, rel))


def test_archive_pending_caps_per_run_and_defers(tmp_path, ingest_eng, adsb_manifest_eng):
    # 3 candidates, cap 2 -> exactly 2 archived this run; the rest stay pending for the next (idempotent drain).
    # The cap is pushed into the SQL (LIMIT), so the un-processed file isn't even loaded.
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    uris = []
    for i in range(3):
        rel = f"bronze/states_raw/dt=2026-06-01/s{i}.parquet"
        _write_parquet(src / rel)
        _insert_ingest(ingest_eng, _src_uri(src, rel))
        uris.append(_src_uri(src, rel))

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14, limit=2,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["files"] == 2
    assert out["ok"] is True
    archived = [u for u in uris if _archived(ingest_eng, "ingestion_manifest", "object_uri", u)]
    assert len(archived) == 2  # exactly the cap; one remains pending for the next run


def test_archive_pending_includes_legacy_states_lane(tmp_path, ingest_eng, adsb_manifest_eng):
    # The pre-states_raw history (bronze/states, a different bucket) is the oldest aged Parquet — prime
    # cold-archive material; _rel_key slices from bronze/ so the dest is bucket-agnostic.
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    rel = "bronze/states/dt=2026-05-17/hr=06/min=00/region=oceania.parquet"
    _write_parquet(src / rel)
    _insert_ingest(ingest_eng, _src_uri(src, rel))

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["files"] == 1
    assert (cold / rel).exists()
    assert _archived(ingest_eng, "ingestion_manifest", "object_uri", _src_uri(src, rel))


def test_archive_pending_interleaves_lanes_under_cap(tmp_path, ingest_eng, adsb_manifest_eng):
    # Both lanes must make progress under the cap — opensky-first ordering would starve adsb behind the
    # larger opensky backlog during the initial drain.
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    for i in range(2):
        rel = f"bronze/states_raw/dt=2026-06-01/s{i}.parquet"
        _write_parquet(src / rel)
        _insert_ingest(ingest_eng, _src_uri(src, rel))
    adsb_rel = "bronze/adsb_state/dt=2026-06-01/a.parquet"
    _write_parquet(src / adsb_rel)
    _insert_adsb(adsb_manifest_eng, "a.parquet", src, adsb_rel)

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14, limit=2,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["files"] == 2
    assert out["more_remaining"] is True
    assert (cold / adsb_rel).exists()
    assert _archived(adsb_manifest_eng, "adsb_ingestion_manifest", "filename", "a.parquet")


def test_archive_pending_fair_across_opensky_prefixes_under_cap(tmp_path, ingest_eng, adsb_manifest_eng):
    # Round-robin must span the OpenSky prefixes too, not just OpenSky-vs-adsb — else flights_raw starves behind
    # the larger states_raw backlog under the cap.
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    for i in range(2):
        rel = f"bronze/states_raw/dt=2026-06-01/s{i}.parquet"
        _write_parquet(src / rel)
        _insert_ingest(ingest_eng, _src_uri(src, rel))
    flights_rel = "bronze/flights_raw/dt=2026-06-01/f.parquet"
    _write_parquet(src / flights_rel)
    _insert_ingest(ingest_eng, _src_uri(src, flights_rel))

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14, limit=2,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["files"] == 2
    assert (cold / flights_rel).exists()  # flights_raw progressed rather than waiting behind both states_raw


def test_archive_pending_more_remaining_false_when_all_fit(tmp_path, ingest_eng, adsb_manifest_eng):
    src = tmp_path / "garage"
    cold = tmp_path / "cold"
    cold.mkdir()
    rel = "bronze/states_raw/dt=2026-06-01/s.parquet"
    _write_parquet(src / rel)
    _insert_ingest(ingest_eng, _src_uri(src, rel))

    out = arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14,
                              engine=ingest_eng, adsb_engine=adsb_manifest_eng)
    assert out["more_remaining"] is False


def test_archive_pending_rejects_nonpositive_limit(tmp_path, ingest_eng, adsb_manifest_eng):
    # A non-positive cap is a misconfig (e.g. ARCHIVE_MAX_FILES=0) — fail fast, before any SQL, rather than
    # silently archiving nothing (LIMIT 0) or behaving dialect-dependently (sqlite -1 = unlimited).
    cold = tmp_path / "cold"
    cold.mkdir()
    for bad in (0, -1):
        with pytest.raises(ValueError, match="must be positive"):
            arc.archive_pending(fs=_FS, cold_path=cold, older_than_days=14, limit=bad,
                                engine=ingest_eng, adsb_engine=adsb_manifest_eng)


def test_rel_key_rejects_path_traversal():
    # A manifest URI with a traversal segment must never resolve outside the cold root.
    assert arc._rel_key("s3://sancha1090/bronze/states_raw/dt=2026-06-01/x.parquet") == \
        "bronze/states_raw/dt=2026-06-01/x.parquet"
    for bad in (
        "s3://sancha1090/bronze/../../etc/passwd",
        "s3://sancha1090/bronze/states_raw/../../../etc/passwd",
        "s3://sancha1090/bronze/states_raw//x.parquet",
    ):
        with pytest.raises(ValueError, match="unsafe path segment"):
            arc._rel_key(bad)


def test_gc_garage_copies_off_by_default_is_noop(tmp_path):
    f = tmp_path / "x.parquet"
    _write_parquet(f)
    out = arc.gc_garage_copies([f"s3://{f}"], fs=_FS)  # confirm defaults False
    assert out["deleted"] == 0
    assert f.exists()


def test_gc_garage_copies_deletes_when_confirmed(tmp_path):
    f = tmp_path / "x.parquet"
    _write_parquet(f)
    out = arc.gc_garage_copies([f"s3://{f}"], fs=_FS, confirm=True)
    assert out["deleted"] == 1
    assert not f.exists()
