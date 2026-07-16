from __future__ import annotations

import os

import psycopg2
import sqlalchemy as sa
from sqlalchemy.engine import URL


def analytics_url() -> URL:
    return URL.create(
        "postgresql+psycopg2",
        username=os.environ["ANALYTICS_PG_USER"],
        password=os.environ["ANALYTICS_PG_PASSWORD"],
        host=os.environ["ANALYTICS_PG_HOST"],
        port=int(os.environ.get("ANALYTICS_PG_PORT", "5432")),
        database=os.environ["ANALYTICS_PG_DB"],
    )


def analytics_engine() -> sa.Engine:
    return sa.create_engine(analytics_url())


def _positive_ms(env_var: str, default: int) -> int:
    # A 0 (or negative/garbage) override would silently disable the guard it's meant to set —
    # connect_timeout=0 waits forever, statement_timeout=0 means no cancel; clamp to >=1.
    try:
        n = int(os.environ.get(env_var, default))
    except ValueError:
        n = default
    return max(1, n)


def rw_connect() -> psycopg2.extensions.connection:
    # connect_timeout bounds TCP setup; statement_timeout is a server-side cancel once a query runs;
    # tcp_user_timeout is the OS deadline for a socket that stops getting ACKs (half-dead-socket case).
    # None of the three fire on an RW catalog-lock DDL wait (live-probed 2026-07-11) — needs an RW kill/restart.
    conn = psycopg2.connect(
        host=os.environ.get("RISINGWAVE_HOST", "risingwave"),
        port=int(os.environ.get("RISINGWAVE_PORT", "4566")),
        user="root",
        dbname="dev",
        connect_timeout=_positive_ms("RISINGWAVE_CONNECT_TIMEOUT", 5),
        options=f"-c statement_timeout={_positive_ms('RISINGWAVE_STATEMENT_TIMEOUT_MS', 60000)}",
        tcp_user_timeout=_positive_ms("RISINGWAVE_TCP_USER_TIMEOUT_MS", 60000),
    )
    conn.autocommit = True
    return conn
