from __future__ import annotations

from typing import Optional

import sqlalchemy as sa

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
    iceberg_committed_at  TIMESTAMPTZ
)
"""

_table_ready = False
_default_engine: Optional[sa.Engine] = None


def _engine() -> sa.Engine:
    # Memoized: an Engine owns a connection pool and is meant to be a long-lived singleton.
    global _default_engine
    if _default_engine is None:
        _default_engine = analytics_engine()
    return _default_engine


def ensure_table(engine: Optional[sa.Engine] = None) -> None:
    global _table_ready
    eng = engine or _engine()
    with eng.begin() as conn:
        conn.execute(sa.text(_DDL))
    if engine is None:
        _table_ready = True


def pending_uris(uri_prefix: str, engine: Optional[sa.Engine] = None) -> list[dict]:
    # Prefix-scoped: the manifest is shared by the states and flights lanes, and each
    # tableize DAG must only drain its own URIs (v5.1).
    eng = engine or _engine()
    if engine is None and not _table_ready:
        ensure_table(eng)
    stmt = sa.text(
        f"""
        SELECT object_uri, snapshot_min, snapshot_max, row_count
          FROM {_TABLE}
         WHERE iceberg_committed_at IS NULL
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


def mark_iceberg_committed(uris: list[str], engine: Optional[sa.Engine] = None) -> int:
    if not uris:
        return 0
    eng = engine or _engine()
    stmt = sa.text(
        f"""
        UPDATE {_TABLE}
           SET iceberg_committed_at = CURRENT_TIMESTAMP
         WHERE object_uri IN :uris
           AND iceberg_committed_at IS NULL
        """
    ).bindparams(sa.bindparam("uris", expanding=True))
    with eng.begin() as conn:
        result = conn.execute(stmt, {"uris": list(uris)})
        return result.rowcount or 0


def record_load(
    object_uri: str,
    snapshot_min: Optional[int],
    snapshot_max: Optional[int],
    row_count: int,
    engine: Optional[sa.Engine] = None,
) -> None:
    eng = engine or _engine()
    if engine is None and not _table_ready:
        ensure_table(eng)
    stmt = sa.text(
        f"""
        INSERT INTO {_TABLE}
            (object_uri, snapshot_min, snapshot_max, row_count)
        VALUES (:uri, :smin, :smax, :rows)
        ON CONFLICT (object_uri) DO NOTHING
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
