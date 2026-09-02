from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa

from include.db import analytics_engine

_TABLE = "adsblol_route_attempts"
_RELEASE_TABLE = "adsblol_release_landings"
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
        # Same ensure_table as the attempt ledger so there is only one readiness flag.
        conn.execute(sa.text(
            f"""
            CREATE TABLE IF NOT EXISTS {_RELEASE_TABLE} (
                trace_day TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                tag TEXT NOT NULL,
                parts INTEGER NOT NULL,
                members INTEGER NOT NULL,
                targets INTEGER NOT NULL,
                landed INTEGER NOT NULL,
                missing INTEGER NOT NULL,
                errors INTEGER NOT NULL,
                landed_at TIMESTAMPTZ NOT NULL
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


def filter_unattempted(
    pairs: list[tuple[str, str]],
    engine: Optional[sa.Engine] = None,
) -> list[tuple[str, str]]:
    # A release tar is streamed once per trace day, so absence in it is final and needs no retry
    # policy; only 'landed' pairs must never re-extract (the resegment script clears them on purpose).
    if not pairs:
        return []
    eng = _prepare(engine)
    days = sorted({d for _, d in pairs})
    stmt = sa.text(
        f"SELECT icao24, trace_day FROM {_TABLE} "
        f"WHERE trace_day IN :days AND outcome = 'landed'"
    ).bindparams(sa.bindparam("days", expanding=True))
    with eng.begin() as conn:
        landed = {(r.icao24, r.trace_day) for r in conn.execute(stmt, {"days": days})}
    return [p for p in pairs if p not in landed]


# Class-1 selector predicate: a lag-<=1 'landed' stamp from before the tar-lane cutover means the
# live-site fetch raced the site's daily assembly, so the landed trace may be truncated mid-air.
# The 'error' arm keeps a failed recovery attempt eligible — its re-stamp is post-cutover by
# construction, so without it an errored pair vanishes and the rc=1-then-rerun contract breaks.
LIVE_ERA_LANDED_SQL = (
    f"SELECT icao24, trace_day FROM {_TABLE} "
    f"WHERE trace_day IN :days AND icao24 IN :hexes "
    f"AND ((outcome = 'landed' AND attempted_at::date - trace_day::date <= 1 "
    f"AND attempted_at < :cutover) OR outcome = 'error')"
)


def landed_live_era(
    pairs: list[tuple[str, str]],
    cutover_iso: str,
    engine: Optional[sa.Engine] = None,
) -> list[tuple[str, str]]:
    if not pairs:
        return []
    eng = _prepare(engine)
    stmt = sa.text(LIVE_ERA_LANDED_SQL).bindparams(
        sa.bindparam("days", expanding=True), sa.bindparam("hexes", expanding=True))
    params = {"days": sorted({d for _, d in pairs}),
              "hexes": sorted({h for h, _ in pairs}), "cutover": cutover_iso}
    with eng.begin() as conn:
        found = {(r.icao24, r.trace_day) for r in conn.execute(stmt, params)}
    return [p for p in pairs if p in found]


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


def release_landing(day_iso: str, engine: Optional[sa.Engine] = None) -> Optional[dict]:
    eng = _prepare(engine)
    stmt = sa.text(
        f"SELECT trace_day, repo, tag, parts, members, targets, landed, missing, errors, landed_at "
        f"FROM {_RELEASE_TABLE} WHERE trace_day = :day"
    )
    with eng.begin() as conn:
        row = conn.execute(stmt, {"day": day_iso}).mappings().first()
    return dict(row) if row is not None else None


def record_release_landing(day_iso: str, *, repo: str, tag: str, parts: int, members: int,
                           targets: int, landed: int, missing: int, errors: int,
                           engine: Optional[sa.Engine] = None) -> int:
    eng = _prepare(engine)
    stmt = sa.text(
        f"""
        INSERT INTO {_RELEASE_TABLE}
            (trace_day, repo, tag, parts, members, targets, landed, missing, errors, landed_at)
        VALUES (:day, :repo, :tag, :parts, :members, :targets, :landed, :missing, :errors, :now)
        ON CONFLICT (trace_day) DO UPDATE SET
            repo = EXCLUDED.repo,
            tag = EXCLUDED.tag,
            parts = EXCLUDED.parts,
            members = EXCLUDED.members,
            targets = EXCLUDED.targets,
            landed = EXCLUDED.landed,
            missing = EXCLUDED.missing,
            errors = EXCLUDED.errors,
            landed_at = EXCLUDED.landed_at
        """
    )
    with eng.begin() as conn:
        conn.execute(stmt, {"day": day_iso, "repo": repo, "tag": tag, "parts": parts,
                            "members": members, "targets": targets, "landed": landed,
                            "missing": missing, "errors": errors,
                            "now": datetime.now(timezone.utc).isoformat()})
    return 1
