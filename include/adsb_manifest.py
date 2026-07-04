from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa

from include.db import analytics_engine


# Seam: tests point this at a schema-less sqlite mirror; production uses the public schema.
_TABLE = "public.adsb_ingestion_manifest"

_DDL = """
CREATE TABLE IF NOT EXISTS public.adsb_ingestion_manifest (
    filename                TEXT        PRIMARY KEY,
    process_uuid            UUID        NOT NULL,
    stream                  TEXT        NOT NULL,
    hostname                TEXT        NOT NULL,
    rotation_start_ts       TIMESTAMPTZ NOT NULL,
    rotation_end_ts         TIMESTAMPTZ NOT NULL,
    complete                BOOLEAN     NOT NULL,
    row_count               BIGINT,
    frame_count             BIGINT,
    byte_count              BIGINT,
    beast_uncompressed_size BIGINT,
    schema_version          INTEGER     NOT NULL,
    s3_uri                  TEXT        NOT NULL,
    manifest_s3_uri         TEXT        NOT NULL,
    landed_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    ch_loaded_at            TIMESTAMPTZ,
    archived_at             TIMESTAMPTZ,
    provenance              TEXT        NOT NULL DEFAULT 'live'
)
"""

# Self-migrate existing prod tables: the CREATE above is a no-op once the table exists. Additive only —
# the manifest is load-bearing (postgres-analytics tenancy), never drop/recreate.
_MIGRATE_DDL = (
    "ALTER TABLE public.adsb_ingestion_manifest ADD COLUMN IF NOT EXISTS ch_loaded_at TIMESTAMPTZ",
    "ALTER TABLE public.adsb_ingestion_manifest ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ",
)

_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS adsb_ingestion_manifest_pending_idx
    ON public.adsb_ingestion_manifest (landed_at)
    WHERE ch_loaded_at IS NULL AND stream = 'adsb_state'
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
        # ADD COLUMN IF NOT EXISTS is Postgres-only; sqlite test tables already carry the columns via _DDL.
        if eng.dialect.name == "postgresql":
            for ddl in _MIGRATE_DDL:
                conn.execute(sa.text(ddl))
        # Index built AFTER the migrations so its ch_loaded_at predicate column is guaranteed present on an older
        # table; DROP first so the repoint off the old iceberg_committed_at predicate self-heals (CREATE INDEX IF
        # NOT EXISTS keeps the existing index by name, so it would never adopt the new predicate otherwise).
        conn.execute(sa.text("DROP INDEX IF EXISTS adsb_ingestion_manifest_pending_idx"))
        conn.execute(sa.text(_INDEX_DDL))
    if engine is None:
        _table_ready = True


def _ensure_once(engine_arg: Optional[sa.Engine]) -> None:
    if engine_arg is None and not _table_ready:
        ensure_table()  # no-arg latches _table_ready so the DDL+ALTER runs once, not per call


def record_bundle(
    *,
    filename: str,
    process_uuid: str,
    stream: str,
    hostname: str,
    rotation_start_ts: str,
    rotation_end_ts: str,
    complete: bool,
    schema_version: int,
    s3_uri: str,
    manifest_s3_uri: str,
    row_count: Optional[int] = None,
    frame_count: Optional[int] = None,
    byte_count: Optional[int] = None,
    beast_uncompressed_size: Optional[int] = None,
    provenance: str = "live",
    engine: Optional[sa.Engine] = None,
) -> None:
    eng = engine or _engine()
    _ensure_once(engine)
    stmt = sa.text(
        f"""
        INSERT INTO {_TABLE}
            (filename, process_uuid, stream, hostname, rotation_start_ts, rotation_end_ts,
             complete, row_count, frame_count, byte_count, beast_uncompressed_size,
             schema_version, s3_uri, manifest_s3_uri, provenance)
        VALUES
            (:filename, :process_uuid, :stream, :hostname, :rotation_start_ts, :rotation_end_ts,
             :complete, :row_count, :frame_count, :byte_count, :beast_uncompressed_size,
             :schema_version, :s3_uri, :manifest_s3_uri, :provenance)
        ON CONFLICT (filename) DO NOTHING
        """
    )
    with eng.begin() as conn:
        conn.execute(stmt, {
            "filename": filename, "process_uuid": process_uuid, "stream": stream,
            "hostname": hostname, "rotation_start_ts": rotation_start_ts,
            "rotation_end_ts": rotation_end_ts, "complete": complete, "row_count": row_count,
            "frame_count": frame_count, "byte_count": byte_count,
            "beast_uncompressed_size": beast_uncompressed_size, "schema_version": schema_version,
            "s3_uri": s3_uri, "manifest_s3_uri": manifest_s3_uri, "provenance": provenance,
        })


def already_ingested(filenames: list[str], engine: Optional[sa.Engine] = None) -> set[str]:
    if not filenames:
        return set()
    eng = engine or _engine()
    _ensure_once(engine)
    stmt = sa.text(
        f"SELECT filename FROM {_TABLE} WHERE filename IN :names"
    ).bindparams(sa.bindparam("names", expanding=True))
    with eng.begin() as conn:
        return {r[0] for r in conn.execute(stmt, {"names": list(filenames)}).fetchall()}


def newest_adsb_rotation_end(engine: Optional[sa.Engine] = None) -> Optional[datetime]:
    """Newest adsb_state close time on record — the stale-check baseline for runs that land
    nothing new, where current-run results can't reveal a silent producer."""
    eng = engine or _engine()
    _ensure_once(engine)
    stmt = sa.text(f"SELECT max(rotation_end_ts) FROM {_TABLE} WHERE stream = 'adsb_state'")
    with eng.begin() as conn:
        val = conn.execute(stmt).scalar()
    if val is None:
        return None
    # Postgres TIMESTAMPTZ returns a datetime; the sqlite test mirror returns the ISO string.
    return datetime.fromisoformat(val.replace("Z", "+00:00")) if isinstance(val, str) else val


def _pending_adsb_uris(marker_col: str, engine: Optional[sa.Engine]) -> list[dict]:
    eng = engine or _engine()
    _ensure_once(engine)
    # marker_col is an internal constant, never user input.
    stmt = sa.text(
        f"""
        SELECT filename, s3_uri
          FROM {_TABLE}
         WHERE stream = 'adsb_state' AND {marker_col} IS NULL
         ORDER BY landed_at
        """
    )
    with eng.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt).fetchall()]


def pending_ch_adsb_uris(engine: Optional[sa.Engine] = None) -> list[dict]:
    return _pending_adsb_uris("ch_loaded_at", engine)


def mark_ch_loaded(filenames: list[str], engine: Optional[sa.Engine] = None) -> int:
    if not filenames:
        return 0
    eng = engine or _engine()
    _ensure_once(engine)
    stmt = sa.text(
        f"""
        UPDATE {_TABLE}
           SET ch_loaded_at = CURRENT_TIMESTAMP
         WHERE filename IN :names
           AND ch_loaded_at IS NULL
        """
    ).bindparams(sa.bindparam("names", expanding=True))
    with eng.begin() as conn:
        return conn.execute(stmt, {"names": list(filenames)}).rowcount or 0


def all_adsb_state_uris(engine: Optional[sa.Engine] = None) -> set[str]:
    # Membership set for the rebuild guard: every adsb_state data URI the manifest has ever registered.
    eng = engine or _engine()
    _ensure_once(engine)
    stmt = sa.text(f"SELECT s3_uri FROM {_TABLE} WHERE stream = 'adsb_state'")
    with eng.begin() as conn:
        return {r[0] for r in conn.execute(stmt).fetchall()}


def pending_archive_adsb_uris(
    older_than_days: int, engine: Optional[sa.Engine] = None, limit: Optional[int] = None
) -> list[dict]:
    # Selects s3_uri (the full key the archiver copies); limit is the per-run cap pushed into SQL to bound the load.
    # Reject a negative LIMIT (sqlite reads it as unlimited, postgres errors); 0 stays valid (no rows).
    if limit is not None and limit < 0:
        raise ValueError(f"pending_archive_adsb_uris: limit must not be negative, got {limit}")
    eng = engine or _engine()
    _ensure_once(engine)
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    # Typed bind so the cutoff compares as a real timestamptz in postgres (not a coerced string literal) and as
    # SQLAlchemy's own datetime string in the sqlite test mirror — correct + warning-free in both.
    params = {"cutoff": cutoff}
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT :limit"
        params["limit"] = limit
    stmt = sa.text(
        f"""
        SELECT filename, s3_uri, row_count
          FROM {_TABLE}
         WHERE stream = 'adsb_state'
           AND ch_loaded_at IS NOT NULL
           AND ch_loaded_at < :cutoff
           AND archived_at IS NULL
         ORDER BY landed_at{limit_sql}
        """
    ).bindparams(sa.bindparam("cutoff", type_=sa.DateTime(timezone=True)))
    with eng.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt, params).fetchall()]


def mark_archived(filenames: list[str], engine: Optional[sa.Engine] = None) -> int:
    if not filenames:
        return 0
    eng = engine or _engine()
    _ensure_once(engine)
    stmt = sa.text(
        f"""
        UPDATE {_TABLE}
           SET archived_at = CURRENT_TIMESTAMP
         WHERE filename IN :names
           AND archived_at IS NULL
        """
    ).bindparams(sa.bindparam("names", expanding=True))
    with eng.begin() as conn:
        return conn.execute(stmt, {"names": list(filenames)}).rowcount or 0
