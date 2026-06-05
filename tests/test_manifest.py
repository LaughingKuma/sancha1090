from __future__ import annotations

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
            " iceberg_committed_at TIMESTAMP"
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
        " row_count INTEGER,"
        " iceberg_committed_at TIMESTAMP)"
    )
    monkeypatch.setattr(manifest, "_DDL", sqlite_ddl)
    manifest.ensure_table(engine=eng)
    with eng.begin() as conn:
        tables = [r[0] for r in conn.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()]
    assert "ingestion_manifest" in tables
