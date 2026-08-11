"""Shared fixtures for DAG and pipeline tests."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from airflow.models import DagBag

from include import adsb_manifest as am
from include import manifest


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
    ch_loaded_at            TIMESTAMP,
    archived_at             TIMESTAMP,
    provenance              TEXT DEFAULT 'live'
)
"""

# Schema-less sqlite mirror of public.ingestion_manifest (the opensky lane's table).
_INGEST_DDL = """
CREATE TABLE ingestion_manifest (
    object_uri   TEXT PRIMARY KEY,
    loaded_at    TIMESTAMP,
    snapshot_min INTEGER,
    snapshot_max INTEGER,
    row_count    INTEGER,
    ch_loaded_at TIMESTAMP,
    archived_at  TIMESTAMP
)
"""


def seed_adsb_bundle(eng, filename, **over):
    # canonical adsb_state bundle; hostname/date literals are cosmetic — nothing asserts them
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


def fake_ch(rows, expect_params=None):
    # canned-rows CH client stand-in; expect_params pins the bound parameters when given
    class _Res:
        result_rows = rows

    class _Client:
        def query(self, _sql, parameters=None, **_kw):
            if expect_params is not None:
                assert parameters == expect_params
            return _Res()

        def close(self):
            pass

    return _Client()


def load_livemap_module(filename):
    # Spec-load preserves the flat image layout; livemap is not installed as a package.
    spec = importlib.util.spec_from_file_location(Path(filename).stem, REPO_ROOT / "livemap" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def dagbag() -> DagBag:
    """Parse the project's DAGs once per test session."""
    return DagBag(dag_folder=str(DAGS_FOLDER), include_examples=False)


@pytest.fixture(scope="session")
def ch_cur():
    # Live ClickHouse cursor-shim for the serving-mart integration tests: skips when CH is unreachable
    # (host / CI without the stack), runs for real inside the airflow containers; missing tables fail RED.
    try:
        import clickhouse_connect
    except ImportError as exc:
        pytest.skip(f"clickhouse-connect not available: {exc}")
    try:
        client = clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
            settings={"join_use_nulls": 1},
        )
        client.query("SELECT 1")
    except clickhouse_connect.driver.exceptions.OperationalError as exc:
        # Only network unreachability skips; config/auth/programming errors fail loudly (RED).
        pytest.skip(f"clickhouse not reachable: {exc}")

    class _Cur:
        # Minimal DBAPI-ish shim so the mart tests can keep `cur.execute(sql); cur.fetchall()`.
        def __init__(self, c):
            self._c = c
            self._rows: list = []

        def execute(self, sql, params=None):
            self._rows = self._c.query(sql, parameters=params or {}).result_rows

        def fetchall(self):
            return self._rows

    try:
        yield _Cur(client)
    finally:
        client.close()


@pytest.fixture(scope="session", autouse=True)
def _private_default_env():
    # The private-mode spec loads scattered across test files must not inherit an ambient
    # LIVEMAP_PUBLIC_MODE (e.g. tests run inside the public container) — the inverse pins would silently flip.
    os.environ.pop("LIVEMAP_PUBLIC_MODE", None)


@pytest.fixture(scope="module")
def livemap_public_mod():
    # LADD serve-time suppression is a PUBLIC-instance obligation, and PUBLIC_MODE is read from env at import —
    # so the public sidecar must be spec-loaded with the env set. os.environ, not monkeypatch: the latter is
    # function-scoped and cannot back a module-scoped fixture. A nonexistent cache path keeps the boot seed
    # deterministic (no stray container cache steering the tests).
    env = {"LIVEMAP_PUBLIC_MODE": "1", "LIVEMAP_LADD_CACHE_PATH": "/nonexistent/ladd_cache.json"}
    prev = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(
            "livemap_app_public", REPO_ROOT / "livemap" / "app.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for key, value in prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return mod


@pytest.fixture(scope="module")
def livemap():
    # fresh private-mode app per test module: tests monkeypatch its module globals
    return load_livemap_module("app.py")


@pytest.fixture
def livemap_public(livemap_public_mod):
    # Public mode registers the per-IP limiter and its buckets are per-instance state — clear them so
    # unrelated tests sharing this module-scoped app can't drain each other's burst into 429s.
    livemap_public_mod.rl._buckets.clear()
    return livemap_public_mod


@pytest.fixture
def adsb_manifest_eng(monkeypatch):
    monkeypatch.setattr(am, "_TABLE", "adsb_ingestion_manifest")
    e = sa.create_engine("sqlite:///:memory:")
    with e.begin() as conn:
        conn.execute(sa.text(_SQLITE_DDL))
    return e


@pytest.fixture
def ingest_eng(monkeypatch):
    monkeypatch.setattr(manifest, "_TABLE", "ingestion_manifest")
    e = sa.create_engine("sqlite:///:memory:")
    with e.begin() as conn:
        conn.execute(sa.text(_INGEST_DDL))
    return e
