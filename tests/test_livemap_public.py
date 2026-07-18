import collections
import importlib.util
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(monkeypatch, public):
    # Fresh module load per mode: PUBLIC_MODE is read from env at import, so the two instances are independent.
    if public:
        monkeypatch.setenv("LIVEMAP_PUBLIC_MODE", "1")
    else:
        monkeypatch.delenv("LIVEMAP_PUBLIC_MODE", raising=False)
    # A missing cache path keeps _ladd_suppress = None (no stray container cache) so /track short-circuits.
    monkeypatch.setenv("LIVEMAP_LADD_CACHE_PATH", "/nonexistent/ladd_cache.json")
    spec = importlib.util.spec_from_file_location(
        f"livemap_public_{public}", REPO_ROOT / "livemap" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def public_app(monkeypatch):
    return _load(monkeypatch, public=True)


@pytest.fixture
def private_app(monkeypatch):
    return _load(monkeypatch, public=False)


# ---- token bucket (pure, fake-clock injected via the `now` arg) ----

def test_token_bucket_burst_then_denied(public_app):
    allow = public_app._rate_limit_allow
    buckets = {}
    t = 1000.0
    assert all(allow("1.2.3.4", t, buckets) for _ in range(10))
    assert allow("1.2.3.4", t, buckets) is False


def test_token_bucket_refills_over_time(public_app):
    allow = public_app._rate_limit_allow
    buckets = {}
    t = 1000.0
    for _ in range(10):
        allow("ip", t, buckets)
    assert allow("ip", t, buckets) is False
    assert allow("ip", t + 1.0, buckets) is True
    assert allow("ip", t + 1.0, buckets) is False


def test_token_bucket_refill_capped_at_burst(public_app):
    allow = public_app._rate_limit_allow
    buckets = {}
    t = 1000.0
    for _ in range(10):
        allow("ip", t, buckets)
    passed = sum(allow("ip", t + 10_000.0, buckets) for _ in range(20))
    assert passed == 10


def test_rate_buckets_hard_cap_evicts_oldest(public_app):
    allow = public_app._rate_limit_allow
    buckets = collections.OrderedDict()
    t = 1000.0
    # 50 always-active distinct IPs through a cap of 10: the map must never exceed the cap even with
    # zero idle time — the distributed-botnet shape the old idle-only sweep could not bound.
    for i in range(50):
        allow(f"ip{i}", t, buckets, max_buckets=10)
        assert len(buckets) <= 10
    assert set(buckets) == {f"ip{i}" for i in range(40, 50)}
    allow("ip40", t, buckets, max_buckets=10)       # touch the OLDEST resident: move-to-end refreshes its recency...
    allow("new", t, buckets, max_buckets=10)
    assert "ip40" in buckets and "ip41" not in buckets   # ...so ip41 is now least-recently-seen and is evicted


# ---- client IP resolution ----

def test_client_ip_prefers_cf_header(public_app):
    ip = public_app._client_ip
    with_cf = types.SimpleNamespace(
        headers={"CF-Connecting-IP": " 1.2.3.4 "}, client=types.SimpleNamespace(host="9.9.9.9")
    )
    assert ip(with_cf) == "1.2.3.4"
    without_cf = types.SimpleNamespace(headers={}, client=types.SimpleNamespace(host="9.9.9.9"))
    assert ip(without_cf) == "9.9.9.9"
    no_client = types.SimpleNamespace(headers={}, client=None)
    assert ip(no_client) == "unknown"


# ---- middleware: public mode ----

def test_public_security_headers_present(public_app):
    r = TestClient(public_app.app).get("/aircraft")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert r.headers["X-Frame-Options"] == "DENY"


def test_public_no_permissive_cors(public_app):
    r = TestClient(public_app.app).get("/aircraft")
    assert "Access-Control-Allow-Origin" not in r.headers


def test_public_aircraft_cache_header(public_app):
    r = TestClient(public_app.app).get("/aircraft")
    assert r.headers["Cache-Control"] == "public, s-maxage=1"


def test_public_rate_limit_trips_after_burst(public_app, monkeypatch):
    monkeypatch.setattr(public_app, "_ladd_suppress", None)   # /track short-circuits to empty, no RW hit
    client = TestClient(public_app.app)
    codes = [client.get("/track/ABC123").status_code for _ in range(11)]
    assert codes[:10] == [200] * 10
    assert codes[10] == 429


def test_public_rate_limit_body_is_generic(public_app, monkeypatch):
    monkeypatch.setattr(public_app, "_ladd_suppress", None)
    client = TestClient(public_app.app)
    for _ in range(10):
        client.get("/track/SECRET1")
    r = client.get("/track/SECRET1")
    assert r.status_code == 429
    assert "SECRET1" not in r.text                  # rate-based, never an identity oracle


def test_public_rate_limit_keys_on_cf_ip(public_app, monkeypatch):
    monkeypatch.setattr(public_app, "_ladd_suppress", None)
    client = TestClient(public_app.app)
    for _ in range(11):
        client.get("/track/A", headers={"CF-Connecting-IP": "10.0.0.1"})
    # a second CF IP still has its own full bucket; the drained one stays limited
    assert client.get("/track/A", headers={"CF-Connecting-IP": "10.0.0.2"}).status_code == 200
    assert client.get("/track/A", headers={"CF-Connecting-IP": "10.0.0.1"}).status_code == 429


def test_public_rate_limit_excludes_memory_endpoints(public_app):
    client = TestClient(public_app.app)
    assert all(client.get("/aircraft").status_code == 200 for _ in range(30))
    assert all(client.get("/history").status_code == 200 for _ in range(30))


# ---- middleware: private mode is byte-identical (no hardening) ----

def test_private_no_security_headers(private_app):
    r = TestClient(private_app.app).get("/aircraft")
    assert "X-Content-Type-Options" not in r.headers
    assert "Referrer-Policy" not in r.headers
    assert "X-Frame-Options" not in r.headers


def test_private_no_aircraft_cache_header(private_app):
    r = TestClient(private_app.app).get("/aircraft")
    assert r.headers.get("Cache-Control") != "public, s-maxage=1"


def test_private_no_rate_limit(private_app, monkeypatch):
    monkeypatch.setattr(private_app, "_ladd_suppress", None)
    client = TestClient(private_app.app)
    assert all(client.get("/track/ABC").status_code == 200 for _ in range(30))


# ---- F7: public /range-outline serves no receiver anchor (center null); the ring still renders ----

def test_public_range_outline_center_null_ring_intact(public_app, monkeypatch):
    ring = [[139.0, 35.0], [140.0, 35.0], [140.0, 36.0]]
    monkeypatch.setattr(public_app, "_outline", ring)
    body = TestClient(public_app.app).get("/range-outline").json()
    assert body["center"] is None
    assert body["ring"] == ring


def test_private_range_outline_center_unchanged(private_app, monkeypatch):
    ring = [[139.0, 35.0], [140.0, 35.0]]
    monkeypatch.setattr(private_app, "_outline", ring)
    body = TestClient(private_app.app).get("/range-outline").json()
    assert body["center"] == [private_app.FEEDER_LON, private_app.FEEDER_LAT]
    assert body["ring"] == ring
