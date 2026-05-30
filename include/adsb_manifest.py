from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

import sqlalchemy as sa


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
    iceberg_committed_at    TIMESTAMPTZ,
    iceberg_snapshot_id     BIGINT,
    provenance              TEXT        NOT NULL DEFAULT 'live'
)
"""

_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS adsb_ingestion_manifest_pending_idx
    ON public.adsb_ingestion_manifest (landed_at)
    WHERE iceberg_committed_at IS NULL AND stream = 'adsb_state'
"""

_table_ready = False
_default_engine: Optional[sa.Engine] = None


def _engine() -> sa.Engine:
    # Memoized: an Engine owns a connection pool and is meant to be a long-lived singleton.
    global _default_engine
    if _default_engine is None:
        url = (
            f"postgresql+psycopg2://"
            f"{os.environ['ANALYTICS_PG_USER']}:{os.environ['ANALYTICS_PG_PASSWORD']}"
            f"@{os.environ['ANALYTICS_PG_HOST']}:{os.environ['ANALYTICS_PG_PORT']}"
            f"/{os.environ['ANALYTICS_PG_DB']}"
        )
        _default_engine = sa.create_engine(url)
    return _default_engine


def ensure_table(engine: Optional[sa.Engine] = None) -> None:
    global _table_ready
    eng = engine or _engine()
    with eng.begin() as conn:
        conn.execute(sa.text(_DDL))
        conn.execute(sa.text(_INDEX_DDL))
    if engine is None:
        _table_ready = True


def _ensure_once(eng: sa.Engine, engine_arg: Optional[sa.Engine]) -> None:
    if engine_arg is None and not _table_ready:
        ensure_table(eng)


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
    _ensure_once(eng, engine)
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
    _ensure_once(eng, engine)
    stmt = sa.text(
        f"SELECT filename FROM {_TABLE} WHERE filename IN :names"
    ).bindparams(sa.bindparam("names", expanding=True))
    with eng.begin() as conn:
        return {r[0] for r in conn.execute(stmt, {"names": list(filenames)}).fetchall()}


def newest_adsb_rotation_end(engine: Optional[sa.Engine] = None) -> Optional[datetime]:
    """Newest adsb_state close time on record — the stale-check baseline for runs that land
    nothing new, where current-run results can't reveal a silent producer."""
    eng = engine or _engine()
    _ensure_once(eng, engine)
    stmt = sa.text(f"SELECT max(rotation_end_ts) FROM {_TABLE} WHERE stream = 'adsb_state'")
    with eng.begin() as conn:
        val = conn.execute(stmt).scalar()
    if val is None:
        return None
    # Postgres TIMESTAMPTZ returns a datetime; the sqlite test mirror returns the ISO string.
    return datetime.fromisoformat(val.replace("Z", "+00:00")) if isinstance(val, str) else val


def pending_adsb_uris(engine: Optional[sa.Engine] = None) -> list[dict]:
    eng = engine or _engine()
    _ensure_once(eng, engine)
    stmt = sa.text(
        f"""
        SELECT filename, s3_uri
          FROM {_TABLE}
         WHERE stream = 'adsb_state' AND iceberg_committed_at IS NULL
         ORDER BY landed_at
        """
    )
    with eng.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt).fetchall()]


def mark_iceberg_committed(
    snapshot_by_filename: dict[str, int], engine: Optional[sa.Engine] = None
) -> int:
    if not snapshot_by_filename:
        return 0
    eng = engine or _engine()
    _ensure_once(eng, engine)
    stmt = sa.text(
        f"""
        UPDATE {_TABLE}
           SET iceberg_committed_at = CURRENT_TIMESTAMP,
               iceberg_snapshot_id = :sid
         WHERE filename = :filename
           AND iceberg_committed_at IS NULL
        """
    )
    updated = 0
    with eng.begin() as conn:
        for filename, sid in snapshot_by_filename.items():
            result = conn.execute(stmt, {"sid": sid, "filename": filename})
            updated += result.rowcount or 0
    return updated
