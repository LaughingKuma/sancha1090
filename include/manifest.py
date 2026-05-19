from __future__ import annotations

import os
from typing import Optional

import sqlalchemy as sa


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


def _engine() -> sa.Engine:
    url = (
        f"postgresql+psycopg2://"
        f"{os.environ['ANALYTICS_PG_USER']}:{os.environ['ANALYTICS_PG_PASSWORD']}"
        f"@{os.environ['ANALYTICS_PG_HOST']}:{os.environ['ANALYTICS_PG_PORT']}"
        f"/{os.environ['ANALYTICS_PG_DB']}"
    )
    return sa.create_engine(url)


def ensure_table(engine: Optional[sa.Engine] = None) -> None:
    global _table_ready
    eng = engine or _engine()
    with eng.begin() as conn:
        conn.execute(sa.text(_DDL))
    if engine is None:
        _table_ready = True


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
        """
        INSERT INTO public.ingestion_manifest
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
