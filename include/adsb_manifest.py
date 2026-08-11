from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import sqlalchemy as sa

from include import pg_ledger
from include.db import analytics_engine


STALE_THRESHOLD = timedelta(hours=2)


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def summarize_results(results: list[Optional[dict]]) -> dict[str, int]:
    landed = sum(1 for r in results if r and r["ok"])
    failed = sum(1 for r in results if r and not r["ok"])
    adsb_landed = sum(1 for r in results if r and r["ok"] and r["stream"] == "adsb_state")
    beast_landed = sum(1 for r in results if r and r["ok"] and r["stream"] == "beast_raw")
    return {"landed": landed, "failed": failed,
            "adsb_landed": adsb_landed, "beast_landed": beast_landed}


# >2 h with no fresh adsb_state close time = a silently dead edge producer/push, which is exactly the
# case with no current-run results — hence the manifest-newest fallback. log.error is the alert.
def maybe_log_stale(results: list[Optional[dict]], now: datetime, logger: logging.Logger,
                    manifest_newest: Optional[datetime] = None) -> bool:
    ends = [_parse_iso(r["rotation_end_ts"]) for r in results
            if r and r["ok"] and r["stream"] == "adsb_state"]
    newest = max(ends) if ends else manifest_newest
    if newest is None:
        return False
    if now - newest > STALE_THRESHOLD:
        logger.error("adsb ingest stale: newest adsb_state rotation_end_ts %s is >%s behind %s",
                     newest.isoformat(), STALE_THRESHOLD, now.isoformat())
        return True
    return False


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
        # Index built AFTER the migrations so its ch_loaded_at predicate column is guaranteed present on an older
        # table; DROP first so the repoint off the old iceberg_committed_at predicate self-heals (CREATE INDEX IF
        # NOT EXISTS keeps the existing index by name, so it would never adopt the new predicate otherwise).
        conn.execute(sa.text("DROP INDEX IF EXISTS adsb_ingestion_manifest_pending_idx"))
        conn.execute(sa.text(_INDEX_DDL))
    if engine is None:
        _table_ready = True


def _ensure_once(engine_arg: Optional[sa.Engine]) -> None:
    pg_ledger.ensure_once(__name__, engine_arg, ensure_table)


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
    return pg_ledger.mark_rows(eng, _TABLE, "filename", "ch_loaded_at", filenames)


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
    stmt, params = pg_ledger.pending_archive_query(
        _TABLE, "filename, s3_uri, row_count", "stream = 'adsb_state'", "landed_at",
        older_than_days, limit, "pending_archive_adsb_uris")
    eng = engine or _engine()
    _ensure_once(engine)
    with eng.begin() as conn:
        return [dict(r._mapping) for r in conn.execute(stmt, params).fetchall()]


def mark_archived(filenames: list[str], engine: Optional[sa.Engine] = None) -> int:
    if not filenames:
        return 0
    eng = engine or _engine()
    _ensure_once(engine)
    return pg_ledger.mark_rows(eng, _TABLE, "filename", "archived_at", filenames)
