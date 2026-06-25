from __future__ import annotations

import pytest
import sqlalchemy as sa

from include import manifest


def _fresh_engine():
    eng = sa.create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE ingestion_manifest ("
            " object_uri TEXT PRIMARY KEY,"
            " loaded_at TIMESTAMP,"
            " snapshot_min INTEGER,"
            " snapshot_max INTEGER,"
            " row_count INTEGER,"
            " ch_loaded_at TIMESTAMP,"
            " archived_at TIMESTAMP"
            ")"
        ))
    return eng


def _insert(eng, uri, smin, smax, rows):
    # mirror record_load against the sqlite shim table (no schema prefix)
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT OR IGNORE INTO ingestion_manifest"
                " (object_uri, snapshot_min, snapshot_max, row_count)"
                " VALUES (:uri, :smin, :smax, :rows)"
            ),
            {"uri": uri, "smin": smin, "smax": smax, "rows": rows},
        )


def _insert_loaded(eng, uri, *, ch_loaded_at=None, archived_at=None, rows=1):
    # Far-past / far-future ch_loaded_at strings keep the age comparison wall-clock- and
    # bind-format-independent (the year dominates lexicographically) in the sqlite mirror.
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO ingestion_manifest"
                " (object_uri, row_count, ch_loaded_at, archived_at)"
                " VALUES (:uri, :rows, :ch, :arc)"
            ),
            {"uri": uri, "rows": rows, "ch": ch_loaded_at, "arc": archived_at},
        )


def test_record_load_is_idempotent_on_uri():
    eng = _fresh_engine()
    _insert(eng, "s3://o/bronze/states_raw/x.parquet", 100, 100, 42)
    _insert(eng, "s3://o/bronze/states_raw/x.parquet", 200, 200, 99)
    with eng.begin() as conn:
        count = conn.execute(sa.text("SELECT count(*) FROM ingestion_manifest")).scalar()
        row_count = conn.execute(sa.text(
            "SELECT row_count FROM ingestion_manifest WHERE object_uri = :u"
        ), {"u": "s3://o/bronze/states_raw/x.parquet"}).scalar()
    assert count == 1
    assert row_count == 42


def test_engine_memoizes_default_engine(monkeypatch):
    calls = 0
    expected = object()

    def fake_engine():
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(manifest, "_default_engine", None)
    monkeypatch.setattr(manifest, "analytics_engine", fake_engine)

    assert manifest._engine() is expected
    assert manifest._engine() is expected
    assert calls == 1


def test_ensure_table_runs_postgres_ddl_against_real_engine(monkeypatch):
    # ensure_table issues the DDL string verbatim; exercise it against sqlite
    # by translating just enough — sqlite tolerates the schema-less DDL.
    eng = sa.create_engine("sqlite:///:memory:")
    sqlite_ddl = (
        "CREATE TABLE IF NOT EXISTS ingestion_manifest ("
        " object_uri TEXT PRIMARY KEY,"
        " loaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        " snapshot_min INTEGER,"
        " snapshot_max INTEGER,"
        " row_count INTEGER)"
    )
    monkeypatch.setattr(manifest, "_DDL", sqlite_ddl)
    manifest.ensure_table(engine=eng)
    with eng.begin() as conn:
        tables = [r[0] for r in conn.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()]
    assert "ingestion_manifest" in tables


def test_pending_uris_scoped_to_lane_prefix(monkeypatch):
    # The manifest is shared by the states and flights lanes — each tableize DAG
    # must only drain its own URIs.
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    _insert(eng, "s3://o/bronze/states_raw/dt=2026-06-10/x.parquet", 100, 100, 1)
    _insert(eng, "s3://o/bronze/flights_raw/dt=2026-06-10/airport=RJTT.parquet", 100, 200, 2)
    _insert(eng, "s3://o/bronze/aircraft_db_raw/dt=2026-06-10/aircraft_db.parquet", None, None, 3)

    states = manifest.pending_ch_uris("bronze/states_raw", engine=eng)
    flights = manifest.pending_ch_uris("bronze/flights_raw", engine=eng)
    aircraft = manifest.pending_ch_uris("bronze/aircraft_db_raw", engine=eng)

    assert [r["object_uri"] for r in states] == ["s3://o/bronze/states_raw/dt=2026-06-10/x.parquet"]
    assert [r["object_uri"] for r in flights] == ["s3://o/bronze/flights_raw/dt=2026-06-10/airport=RJTT.parquet"]
    assert [r["object_uri"] for r in aircraft] == ["s3://o/bronze/aircraft_db_raw/dt=2026-06-10/aircraft_db.parquet"]


def test_pending_uris_tolerates_surrounding_slashes(monkeypatch):
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    _insert(eng, "s3://o/bronze/flights_raw/dt=2026-06-10/airport=RJAA.parquet", 1, 2, 5)
    assert len(manifest.pending_ch_uris("/bronze/flights_raw/", engine=eng)) == 1


def test_mark_ch_loaded_is_idempotent_and_scoped(monkeypatch):
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    _insert(eng, "s3://o/bronze/states_raw/dt=2026-06-10/a.parquet", 1, 1, 1)
    _insert(eng, "s3://o/bronze/states_raw/dt=2026-06-10/b.parquet", 2, 2, 2)

    n1 = manifest.mark_ch_loaded(["s3://o/bronze/states_raw/dt=2026-06-10/a.parquet"], engine=eng)
    n2 = manifest.mark_ch_loaded(["s3://o/bronze/states_raw/dt=2026-06-10/a.parquet"], engine=eng)
    assert n1 == 1
    assert n2 == 0  # already loaded

    ch_pending = [r["object_uri"] for r in manifest.pending_ch_uris("bronze/states_raw", engine=eng)]
    assert ch_pending == ["s3://o/bronze/states_raw/dt=2026-06-10/b.parquet"]


def test_mark_ch_loaded_empty_is_noop():
    assert manifest.mark_ch_loaded([], engine=_fresh_engine()) == 0


def test_pending_archive_uris_selects_aged_loaded_unarchived(monkeypatch):
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    _insert_loaded(eng, "s3://o/bronze/states_raw/dt=2026-06-01/aged.parquet", ch_loaded_at="2020-01-01 00:00:00")
    _insert_loaded(eng, "s3://o/bronze/states_raw/dt=2026-06-01/recent.parquet", ch_loaded_at="2099-01-01 00:00:00")
    _insert_loaded(eng, "s3://o/bronze/states_raw/dt=2026-06-01/never.parquet", ch_loaded_at=None)
    _insert_loaded(eng, "s3://o/bronze/states_raw/dt=2026-06-01/done.parquet",
                   ch_loaded_at="2020-01-01 00:00:00", archived_at="2021-01-01 00:00:00")
    _insert_loaded(eng, "s3://o/bronze/flights_raw/dt=2026-06-01/other.parquet", ch_loaded_at="2020-01-01 00:00:00")

    rows = manifest.pending_archive_uris("bronze/states_raw", older_than_days=14, engine=eng)
    assert [r["object_uri"] for r in rows] == ["s3://o/bronze/states_raw/dt=2026-06-01/aged.parquet"]
    assert rows[0]["row_count"] == 1


def test_mark_archived_is_idempotent_and_clears_pending(monkeypatch):
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    _insert_loaded(eng, "s3://o/bronze/states_raw/dt=2026-06-01/a.parquet", ch_loaded_at="2020-01-01 00:00:00")

    n1 = manifest.mark_archived(["s3://o/bronze/states_raw/dt=2026-06-01/a.parquet"], engine=eng)
    n2 = manifest.mark_archived(["s3://o/bronze/states_raw/dt=2026-06-01/a.parquet"], engine=eng)
    assert n1 == 1
    assert n2 == 0  # already archived
    assert manifest.pending_archive_uris("bronze/states_raw", older_than_days=14, engine=eng) == []


def test_mark_archived_empty_is_noop():
    assert manifest.mark_archived([], engine=_fresh_engine()) == 0


def test_pending_archive_uris_respects_limit(monkeypatch):
    # The per-run cap must bound the SQL load, not just slice in Python (a huge backlog mustn't materialize whole).
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    for i in range(3):
        _insert_loaded(eng, f"s3://o/bronze/states_raw/dt=2026-06-01/a{i}.parquet", ch_loaded_at="2020-01-01 00:00:00")
    assert len(manifest.pending_archive_uris("bronze/states_raw", older_than_days=14, engine=eng, limit=2)) == 2


def test_pending_archive_uris_rejects_negative_limit(monkeypatch):
    # A negative LIMIT is the dialect footgun (sqlite = unlimited, postgres errors) — reject at the helper too,
    # not only in archive_pending. LIMIT 0 stays valid (no rows).
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    with pytest.raises(ValueError, match="limit must not be negative"):
        manifest.pending_archive_uris("bronze/states_raw", older_than_days=14, engine=eng, limit=-1)
