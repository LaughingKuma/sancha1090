from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa

# Code sharing ONLY — the two manifest TABLES stay separate by standing decision; helpers take the
# caller's module by name so the per-module globals stay the seams the tests monkeypatch.


def module_engine(mod_name: str, factory) -> sa.Engine:
    # Memoized: an Engine owns a connection pool and is meant to be a long-lived singleton.
    mod = sys.modules[mod_name]
    if mod._default_engine is None:
        mod._default_engine = factory()
    return mod._default_engine


def ensure_once(mod_name: str, engine_arg: Optional[sa.Engine], ensure) -> None:
    # Only the no-arg (production) path latches _table_ready — via the module's own ensure fn — so the
    # DDL+ALTER runs once, not per call; an explicit engine is the tests' sqlite mirror, never latched.
    if engine_arg is None and not sys.modules[mod_name]._table_ready:
        ensure()


def mark_rows(eng: sa.Engine, table: str, key_col: str, marker_col: str, keys: list[str]) -> int:
    # key_col/marker_col are internal constants, never user input.
    stmt = sa.text(
        f"""
        UPDATE {table}
           SET {marker_col} = CURRENT_TIMESTAMP
         WHERE {key_col} IN :keys
           AND {marker_col} IS NULL
        """
    ).bindparams(sa.bindparam("keys", expanding=True))
    with eng.begin() as conn:
        return conn.execute(stmt, {"keys": list(keys)}).rowcount or 0


def pending_archive_query(
    table: str, select_cols: str, lane_where: str, order_col: str,
    older_than_days: int, limit: Optional[int], label: str, params: Optional[dict] = None,
):
    # limit is the caller's per-run cap pushed into SQL so a large backlog never materializes whole. Reject a
    # negative LIMIT (sqlite reads it as unlimited, postgres errors); 0 stays valid (no rows).
    if limit is not None and limit < 0:
        raise ValueError(f"{label}: limit must not be negative, got {limit}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    params = {"cutoff": cutoff, **(params or {})}
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT :limit"
        params["limit"] = limit
    # Typed bind so the cutoff compares as a real timestamptz in postgres (not a coerced string literal) and as
    # SQLAlchemy's own datetime string in the sqlite test mirror — correct + warning-free in both.
    stmt = sa.text(
        f"""
        SELECT {select_cols}
          FROM {table}
         WHERE {lane_where}
           AND ch_loaded_at IS NOT NULL
           AND ch_loaded_at < :cutoff
           AND archived_at IS NULL
         ORDER BY {order_col}{limit_sql}
        """
    ).bindparams(sa.bindparam("cutoff", type_=sa.DateTime(timezone=True)))
    return stmt, params
