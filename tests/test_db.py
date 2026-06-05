from __future__ import annotations

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
