from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa


_DDL = """
CREATE TABLE IF NOT EXISTS public.pipeline_watermarks (
    pipeline_name      TEXT PRIMARY KEY,
    max_snapshot_time  TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
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


def get_or_seed(
    pipeline_name: str, seed_offset: timedelta, engine: Optional[sa.Engine] = None
) -> datetime:
    eng = engine or _engine()
    if engine is None and not _table_ready:
        ensure_table(eng)
    seed_ts = datetime.now(timezone.utc) - seed_offset
    with eng.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO public.pipeline_watermarks (pipeline_name, max_snapshot_time)
                VALUES (:p, :ts)
                ON CONFLICT (pipeline_name) DO NOTHING
                """
            ),
            {"p": pipeline_name, "ts": seed_ts},
        )
        row = conn.execute(
            sa.text("SELECT max_snapshot_time FROM public.pipeline_watermarks WHERE pipeline_name = :p"),
            {"p": pipeline_name},
        ).fetchone()
    return row[0]


def advance(pipeline_name: str, new_max: datetime, conn) -> None:
    conn.execute(
        sa.text(
            """
            UPDATE public.pipeline_watermarks
               SET max_snapshot_time = :ts, updated_at = now()
             WHERE pipeline_name = :p
               AND :ts > max_snapshot_time
            """
        ),
        {"p": pipeline_name, "ts": new_max},
    )
