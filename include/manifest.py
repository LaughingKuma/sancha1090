from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        # ADD COLUMN IF NOT EXISTS is Postgres-only; sqlite test tables already carry the columns via _DDL.
        if eng.dialect.name == "postgresql":
            for ddl in _MIGRATE_DDL:
                conn.execute(sa.text(ddl))
    if engine is None:
        _table_ready = True


def _pending_uris(uri_prefix: str, marker_col: str, engine: Optional[sa.Engine]) -> list[dict]:
    eng = engine or _engine()
    if engine is None and not _table_ready:
        ensure_table()  # no-arg latches _table_ready so the DDL+ALTER runs once, not per call
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
    eng = engine or _engine()
    # marker_col is an internal constant, never user input.
    stmt = sa.text(
        f"""
        UPDATE {_TABLE}
           SET {marker_col} = CURRENT_TIMESTAMP
         WHERE object_uri IN :uris
           AND {marker_col} IS NULL
        """
    ).bindparams(sa.bindparam("uris", expanding=True))
    with eng.begin() as conn:
        result = conn.execute(stmt, {"uris": list(uris)})
        return result.rowcount or 0


def mark_ch_loaded(uris: list[str], engine: Optional[sa.Engine] = None) -> int:
    return _mark_loaded(uris, "ch_loaded_at", engine)


def mark_archived(uris: list[str], engine: Optional[sa.Engine] = None) -> int:
    return _mark_loaded(uris, "archived_at", engine)


def pending_archive_uris(
    uri_prefix: str, older_than_days: int, engine: Optional[sa.Engine] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    # limit is the caller's per-run cap pushed into SQL so a large backlog never materializes whole. Reject a
    # negative LIMIT (sqlite reads it as unlimited, postgres errors); 0 stays valid (no rows).
    if limit is not None and limit < 0:
        raise ValueError(f"pending_archive_uris: limit must not be negative, got {limit}")
    eng = engine or _engine()
    if engine is None and not _table_ready:
        ensure_table()  # no-arg latches _table_ready so the DDL+ALTER runs once, not per call
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    # Typed bind so the cutoff compares as a real timestamptz in postgres (not a coerced string literal) and as
    # SQLAlchemy's own datetime string in the sqlite test mirror — correct + warning-free in both.
    params = {"cutoff": cutoff, "prefix": None}
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT :limit"
        params["limit"] = limit
    stmt = sa.text(
        f"""
        SELECT object_uri, row_count
          FROM {_TABLE}
         WHERE ch_loaded_at IS NOT NULL
           AND ch_loaded_at < :cutoff
           AND archived_at IS NULL
           AND object_uri LIKE :prefix ESCAPE '\\'
         ORDER BY loaded_at{limit_sql}
        """
    ).bindparams(sa.bindparam("cutoff", type_=sa.DateTime(timezone=True)))
    # Escape LIKE wildcards so e.g. the _ in "flights_raw" matches literally, not any char.
    escaped = uri_prefix.strip("/").replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    params["prefix"] = f"%/{escaped}/%"
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
    if engine is None and not _table_ready:
        ensure_table()  # no-arg latches _table_ready so the DDL+ALTER runs once, not per call
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
