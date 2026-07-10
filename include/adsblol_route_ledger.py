from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import sqlalchemy as sa

from include.db import analytics_engine

_TABLE = "adsblol_route_attempts"
_table_ready = False


def ensure_table(engine: Optional[sa.Engine] = None) -> None:
    global _table_ready
    eng = engine or analytics_engine()
    with eng.begin() as conn:
        conn.execute(sa.text(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                icao24 TEXT NOT NULL,
                trace_day TEXT NOT NULL,
                outcome TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                attempted_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (icao24, trace_day)
            )
            """
        ))
    if engine is None:
        _table_ready = True


def _prepare(engine: Optional[sa.Engine]) -> sa.Engine:
    eng = engine or analytics_engine()
    if engine is not None or not _table_ready:
        ensure_table(eng if engine is not None else None)
    return eng


def filter_unattempted(pairs: list[tuple[str, str]], engine: Optional[sa.Engine] = None, *,
                       retry_after_days: int = 7, max_attempts: int = 2) -> list[tuple[str, str]]:
    # 'missing' gets ONE aged retry (adsb.lol occasionally publishes late); 'landed'
    # never refetches — the bronze RMT would only re-collapse identical rows.
    if not pairs:
        return []
    eng = _prepare(engine)
    days = sorted({d for _, d in pairs})
    stmt = sa.text(
        f"SELECT icao24, trace_day, outcome, attempts, attempted_at FROM {_TABLE} "
        f"WHERE trace_day IN :days"
    ).bindparams(sa.bindparam("days", expanding=True))
    with eng.begin() as conn:
        seen = {(r.icao24, r.trace_day): r for r in conn.execute(stmt, {"days": days})}

    now = datetime.now(timezone.utc)
    keep: list[tuple[str, str]] = []
    for hexid, day in pairs:
        row = seen.get((hexid, day))
        if row is None:
            keep.append((hexid, day))
            continue
        if row.outcome == "landed" or row.attempts >= max_attempts:
            continue
        attempted = row.attempted_at
        if isinstance(attempted, str):
            attempted = datetime.fromisoformat(attempted)
        if attempted.tzinfo is None:
            attempted = attempted.replace(tzinfo=timezone.utc)
        if now - attempted >= timedelta(days=retry_after_days):
            keep.append((hexid, day))
    return keep


def delete_attempts(pairs: list[tuple[str, str]], engine: Optional[sa.Engine] = None) -> int:
    # Backfill re-segment: drop 'landed' ledger rows so filter_unattempted lets the pair refetch.
    if not pairs:
        return 0
    eng = _prepare(engine)
    by_day: dict[str, list[str]] = {}
    for icao24, day in pairs:
        by_day.setdefault(day, []).append(icao24)
    stmt = sa.text(
        f"DELETE FROM {_TABLE} WHERE trace_day = :day AND icao24 IN :hexes"
    ).bindparams(sa.bindparam("hexes", expanding=True))
    deleted = 0
    with eng.begin() as conn:
        for day, hexes in by_day.items():
            deleted += conn.execute(stmt, {"day": day, "hexes": hexes}).rowcount or 0
    return deleted


def record_attempts(rows: list[tuple[str, str, str]], engine: Optional[sa.Engine] = None) -> int:
    if not rows:
        return 0
    eng = _prepare(engine)
    stmt = sa.text(
        f"""
        INSERT INTO {_TABLE} (icao24, trace_day, outcome, attempts, attempted_at)
        VALUES (:icao24, :day, :outcome, 1, :now)
        ON CONFLICT (icao24, trace_day) DO UPDATE SET
            outcome = EXCLUDED.outcome,
            attempts = {_TABLE}.attempts + 1,
            attempted_at = EXCLUDED.attempted_at
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    with eng.begin() as conn:
        for icao24, day, outcome in rows:
            conn.execute(stmt, {"icao24": icao24, "day": day, "outcome": outcome, "now": now})
    return len(rows)
