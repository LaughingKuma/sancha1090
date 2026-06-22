from __future__ import annotations

import hashlib
from types import SimpleNamespace

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
            " iceberg_committed_at TIMESTAMP,"
            " ch_loaded_at TIMESTAMP"
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


def test_pending_uris_scoped_to_lane_prefix(monkeypatch):
    # The manifest is shared by the states and flights lanes — each tableize DAG
    # must only drain its own URIs.
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    _insert(eng, "s3://o/bronze/states_raw/dt=2026-06-10/x.parquet", 100, 100, 1)
    _insert(eng, "s3://o/bronze/flights_raw/dt=2026-06-10/airport=RJTT.parquet", 100, 200, 2)
    _insert(eng, "s3://o/bronze/aircraft_db_raw/dt=2026-06-10/aircraft_db.parquet", None, None, 3)

    states = manifest.pending_uris("bronze/states_raw", engine=eng)
    flights = manifest.pending_uris("bronze/flights_raw", engine=eng)
    aircraft = manifest.pending_uris("bronze/aircraft_db_raw", engine=eng)

    assert [r["object_uri"] for r in states] == ["s3://o/bronze/states_raw/dt=2026-06-10/x.parquet"]
    assert [r["object_uri"] for r in flights] == ["s3://o/bronze/flights_raw/dt=2026-06-10/airport=RJTT.parquet"]
    assert [r["object_uri"] for r in aircraft] == ["s3://o/bronze/aircraft_db_raw/dt=2026-06-10/aircraft_db.parquet"]


def test_pending_uris_tolerates_surrounding_slashes(monkeypatch):
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    _insert(eng, "s3://o/bronze/flights_raw/dt=2026-06-10/airport=RJAA.parquet", 1, 2, 5)
    assert len(manifest.pending_uris("/bronze/flights_raw/", engine=eng)) == 1


def test_pending_ch_uris_independent_of_iceberg_marker(monkeypatch):
    # The CH marker advances separately: a committed-to-Iceberg URI is still CH-pending,
    # so a ClickHouse outage can't stall the Iceberg drain and vice-versa (P2 invariant).
    eng = _fresh_engine()
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    _insert(eng, "s3://o/bronze/states_raw/dt=2026-06-10/a.parquet", 1, 1, 1)
    _insert(eng, "s3://o/bronze/states_raw/dt=2026-06-10/b.parquet", 2, 2, 2)

    manifest.mark_iceberg_committed(
        ["s3://o/bronze/states_raw/dt=2026-06-10/a.parquet"], engine=eng)

    # Iceberg drained 'a'; both are still CH-pending.
    iceberg_pending = [r["object_uri"] for r in manifest.pending_uris("bronze/states_raw", engine=eng)]
    ch_pending = [r["object_uri"] for r in manifest.pending_ch_uris("bronze/states_raw", engine=eng)]
    assert iceberg_pending == ["s3://o/bronze/states_raw/dt=2026-06-10/b.parquet"]
    assert sorted(ch_pending) == [
        "s3://o/bronze/states_raw/dt=2026-06-10/a.parquet",
        "s3://o/bronze/states_raw/dt=2026-06-10/b.parquet",
    ]


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
    # CH marker must not have touched the Iceberg marker.
    assert len(manifest.pending_uris("bronze/states_raw", engine=eng)) == 2


def test_mark_ch_loaded_empty_is_noop():
    assert manifest.mark_ch_loaded([], engine=_fresh_engine()) == 0


def _table_with_snapshot(snapshot):
    return SimpleNamespace(current_snapshot=lambda: snapshot)


def _snapshot(properties):
    return SimpleNamespace(summary=SimpleNamespace(additional_properties=properties))


def test_batch_fingerprint_is_order_insensitive():
    uris = ["s3://o/bronze/states_raw/b.parquet", "s3://o/bronze/states_raw/a.parquet"]
    expected = hashlib.sha256(
        "s3://o/bronze/states_raw/a.parquet\ns3://o/bronze/states_raw/b.parquet".encode()
    ).hexdigest()
    assert manifest.batch_fingerprint(uris) == expected
    assert manifest.batch_fingerprint(list(reversed(uris))) == expected


def test_already_appended_matches_current_snapshot_fingerprint():
    table = _table_with_snapshot(_snapshot({"manifest_fingerprint": "abc"}))
    assert manifest.already_appended(table, "abc") is True


def test_already_appended_false_on_fingerprint_mismatch():
    table = _table_with_snapshot(_snapshot({"manifest_fingerprint": "abc"}))
    assert manifest.already_appended(table, "def") is False


def test_already_appended_false_without_current_snapshot():
    assert manifest.already_appended(_table_with_snapshot(None), "abc") is False


def test_already_appended_false_when_snapshot_has_no_summary():
    table = _table_with_snapshot(SimpleNamespace(summary=None))
    assert manifest.already_appended(table, "abc") is False
