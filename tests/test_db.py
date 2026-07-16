from __future__ import annotations

import pytest

from include import db
from include.db import analytics_url


def test_analytics_url_preserves_reserved_password_chars(monkeypatch):
    password = "p@ss:w/rd%#"
    monkeypatch.setenv("ANALYTICS_PG_USER", "analytics")
    monkeypatch.setenv("ANALYTICS_PG_PASSWORD", password)
    monkeypatch.setenv("ANALYTICS_PG_HOST", "postgres-analytics")
    monkeypatch.setenv("ANALYTICS_PG_PORT", "5432")
    monkeypatch.setenv("ANALYTICS_PG_DB", "analytics")

    url = analytics_url()

    assert url.username == "analytics"
    assert url.password == password
    assert url.host == "postgres-analytics"
    assert url.port == 5432
    assert url.database == "analytics"


class _FakeConn:
    def __init__(self):
        self.autocommit = False


def test_rw_connect_default_timeouts(monkeypatch):
    for var in ("RISINGWAVE_CONNECT_TIMEOUT", "RISINGWAVE_STATEMENT_TIMEOUT_MS",
                "RISINGWAVE_TCP_USER_TIMEOUT_MS", "RISINGWAVE_HOST", "RISINGWAVE_PORT"):
        monkeypatch.delenv(var, raising=False)
    seen = {}
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kw: seen.update(kw) or _FakeConn())

    conn = db.rw_connect()

    assert seen["host"] == "risingwave"
    assert seen["port"] == 4566
    assert seen["connect_timeout"] == 5  # sane default: bounds a half-dead socket, not a slow one
    assert seen["options"] == "-c statement_timeout=60000"  # generous for the ~7k-row route insert
    assert seen["tcp_user_timeout"] == 60000  # OS deadline on a socket that stops getting ACKs
    assert conn.autocommit is True


def test_rw_connect_env_overrides(monkeypatch):
    monkeypatch.setenv("RISINGWAVE_HOST", "rw-host")
    monkeypatch.setenv("RISINGWAVE_PORT", "5555")
    monkeypatch.setenv("RISINGWAVE_CONNECT_TIMEOUT", "2")
    monkeypatch.setenv("RISINGWAVE_STATEMENT_TIMEOUT_MS", "15000")
    monkeypatch.setenv("RISINGWAVE_TCP_USER_TIMEOUT_MS", "20000")
    seen = {}
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kw: seen.update(kw) or _FakeConn())

    db.rw_connect()

    assert seen["host"] == "rw-host"
    assert seen["port"] == 5555
    assert seen["connect_timeout"] == 2
    assert seen["options"] == "-c statement_timeout=15000"
    assert seen["tcp_user_timeout"] == 20000


@pytest.mark.parametrize("bad_value", ["0", "-5"])
def test_rw_connect_nonpositive_env_clamps_to_one(monkeypatch, bad_value):
    monkeypatch.setenv("RISINGWAVE_CONNECT_TIMEOUT", bad_value)
    monkeypatch.setenv("RISINGWAVE_STATEMENT_TIMEOUT_MS", bad_value)
    monkeypatch.setenv("RISINGWAVE_TCP_USER_TIMEOUT_MS", bad_value)
    seen = {}
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kw: seen.update(kw) or _FakeConn())

    db.rw_connect()

    # A valid-but-nonpositive override would otherwise silently disable the guard it's meant to
    # set (0 = infinite connect wait / never cancel) — clamped to the floor of 1, not the default.
    assert seen["connect_timeout"] == 1
    assert seen["options"] == "-c statement_timeout=1"
    assert seen["tcp_user_timeout"] == 1


def test_rw_connect_garbage_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RISINGWAVE_CONNECT_TIMEOUT", "not-a-number")
    monkeypatch.setenv("RISINGWAVE_STATEMENT_TIMEOUT_MS", "not-a-number")
    monkeypatch.setenv("RISINGWAVE_TCP_USER_TIMEOUT_MS", "not-a-number")
    seen = {}
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kw: seen.update(kw) or _FakeConn())

    db.rw_connect()

    # Unparseable -> falls back to the (positive) default rather than clamping or raising.
    assert seen["connect_timeout"] == 5
    assert seen["options"] == "-c statement_timeout=60000"
    assert seen["tcp_user_timeout"] == 60000
