from __future__ import annotations

from typing import Optional

import sqlalchemy as sa

from include import pg_ledger
from include.db import analytics_engine


# Seam: tests point this at a schema-less sqlite mirror; production uses the public schema.
_TABLE = "public.ingestion_manifest"

_DDL = """
CREATE TABLE IF NOT EXISTS public.ingestion_manifest (
    object_uri            TEXT PRIMARY KEY,
    loaded_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    snapshot_min          BIGINT,
    snapshot_max          BIGINT,
    row_count             INTEGER,
    ch_loaded_at          TIMESTAMPTZ,
    archived_at           TIMESTAMPTZ
)
"""

# Self-migrate existing prod tables: the CREATE above is a no-op once the table exists. Additive only —
# the manifest is load-bearing (postgres-analytics tenancy), never drop/recreate.
_MIGRATE_DDL = (
    "ALTER TABLE public.ingestion_manifest ADD COLUMN IF NOT EXISTS ch_loaded_at TIMESTAMPTZ",
    "ALTER TABLE public.ingestion_manifest ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ",
)

_table_ready = False
_default_engine: Optional[sa.Engine] = None


def _engine() -> sa.Engine:
    return pg_ledger.module_engine(__name__, analytics_engine)


def ensure_table(engine: Optional[sa.Engine] = None) -> None:
    global _table_ready
    eng = engine or _engine()
    with eng.begin() as conn:
        conn.execute(sa.text(_DDL))
        # ADD COLUMN IF NOT EXISTS is Postgres-only; sqlite test tables already carry the columns via _DDL.
        if eng.dialect.name == "postgresql":
            for ddl in _MIGRATE_DDL:
                conn.execute(sa.text(ddl))
    if engine is None:
        _table_ready = True


def _pending_uris(uri_prefix: str, marker_col: str, engine: Optional[sa.Engine]) -> list[dict]:
    eng = engine or _engine()
    pg_ledger.ensure_once(__name__, engine, ensure_table)
    # marker_col is an internal constant, never user input.
    stmt = sa.text(
        f"""
        SELECT object_uri, snapshot_min, snapshot_max, row_count
          FROM {_TABLE}
         WHERE {marker_col} IS NULL
           AND object_uri LIKE :prefix ESCAPE '\\'
         ORDER BY loaded_at
        """
    )
    # Escape LIKE wildcards so e.g. the _ in "flights_raw" matches literally, not any char.
    escaped = uri_prefix.strip("/").replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    with eng.begin() as conn:
        return [
            dict(r._mapping)
            for r in conn.execute(stmt, {"prefix": f"%/{escaped}/%"}).fetchall()
        ]


def pending_ch_uris(uri_prefix: str, engine: Optional[sa.Engine] = None) -> list[dict]:
    # Prefix-scoped: the manifest is shared by the states and flights lanes, and each
    # tableize DAG must only drain its own URIs (v5.1).
    return _pending_uris(uri_prefix, "ch_loaded_at", engine)


def _mark_loaded(uris: list[str], marker_col: str, engine: Optional[sa.Engine]) -> int:
    if not uris:
        return 0
    return pg_ledger.mark_rows(engine or _engine(), _TABLE, "object_uri", marker_col, uris)


def mark_ch_loaded(uris: list[str], engine: Optional[sa.Engine] = None) -> int:
    return _mark_loaded(uris, "ch_loaded_at", engine)


def mark_archived(uris: list[str], engine: Optional[sa.Engine] = None) -> int:
    return _mark_loaded(uris, "archived_at", engine)


def pending_archive_uris(
    uri_prefix: str, older_than_days: int, engine: Optional[sa.Engine] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    # Escape LIKE wildcards so e.g. the _ in "flights_raw" matches literally, not any char.
    escaped = uri_prefix.strip("/").replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    stmt, params = pg_ledger.pending_archive_query(
        _TABLE, "object_uri, row_count", "object_uri LIKE :prefix ESCAPE '\\'", "loaded_at",
        older_than_days, limit, "pending_archive_uris", params={"prefix": f"%/{escaped}/%"})
    eng = engine or _engine()
    pg_ledger.ensure_once(__name__, engine, ensure_table)
    with eng.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt, params).fetchall()]


def record_load(
    object_uri: str,
    snapshot_min: Optional[int],
    snapshot_max: Optional[int],
    row_count: int,
    engine: Optional[sa.Engine] = None,
) -> None:
    eng = engine or _engine()
    pg_ledger.ensure_once(__name__, engine, ensure_table)
    # A retry/re-fetch rewrites the same key with fresh data — the record follows the object, else
    # the frozen first-write count trips the NAS archiver's rowcount gate. Lifecycle markers
    # (loaded_at, ch_loaded_at, archived_at) are untouched: rewrites change content, not state.
    stmt = sa.text(
        f"""
        INSERT INTO {_TABLE}
            (object_uri, snapshot_min, snapshot_max, row_count)
        VALUES (:uri, :smin, :smax, :rows)
        ON CONFLICT (object_uri) DO UPDATE SET
            snapshot_min = EXCLUDED.snapshot_min,
            snapshot_max = EXCLUDED.snapshot_max,
            row_count    = EXCLUDED.row_count
        """
    )
    with eng.begin() as conn:
        conn.execute(
            stmt,
            {
                "uri": object_uri,
                "smin": snapshot_min,
                "smax": snapshot_max,
                "rows": row_count,
            },
        )
