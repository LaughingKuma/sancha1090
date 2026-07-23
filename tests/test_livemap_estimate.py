import datetime
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def livemap():
    spec = importlib.util.spec_from_file_location("livemap_app", REPO_ROOT / "livemap" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def allow_path_auth(livemap, monkeypatch):
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    monkeypatch.setattr(
        livemap,
        "_fetch_path_auth",
        lambda _fid: ("abc123", "ANA1", False, 1765500000, 1765503600, datetime.date(2026, 6, 1)),
    )
    # Far-future head keeps the existing settled-path classification stable for this harness.
    monkeypatch.setattr(livemap, "_path_head", {"expiry": float("inf"), "head": datetime.date(2100, 1, 1)})


def test_fetch_od_maps_reconciled_row(livemap, monkeypatch):
    class _Res:
        result_rows = [(35.55, 139.78, "vrs_routes", "unanimous", None, None, None, None)]

    class _Client:
        def query(self, *_a, **_k):
            return _Res()

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_ch_client", lambda: _Client())
    od = livemap._fetch_od(42)
    assert od.origin.lat == 35.55 and od.origin.agreement == "unanimous"
    assert od.dest.lat is None


AUTH = ("abc123", "ANA1", False, 1765500000, 1765503600, datetime.date(2026, 6, 1))
POINTS = [
    (1765500000, 35.0, 140.0, 30000, False, 450, 90, "opensky"),
    (1765500060, 35.0, 140.1, 30000, False, 450, 90, "opensky"),
]


def _loader_result(status="settled", points=POINTS, auth=AUTH, as_of=1765500060):
    return {"status": status, "points": points, "auth": auth, "as_of": as_of}


def _stub_loader(livemap, monkeypatch, result):
    async def load(_flight_id):
        return result

    monkeypatch.setattr(livemap, "_load_path_input", load)
    monkeypatch.setattr(livemap, "_est_cache", {})


def _assert_no_store(response):
    assert response.headers["cache-control"] == "no-store"


def test_path_estimate_settled_serves_and_enqueues_one_group(livemap, monkeypatch):
    _stub_loader(livemap, monkeypatch, _loader_result())
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: livemap.est.OD())
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    response = TestClient(livemap.app).get("/path/42/estimate")

    assert response.status_code == 200
    assert response.json()["segments"]
    assert len(enqueued) == 1
    kind_idx = livemap.ess.INSERT_COLUMNS.index("kind")
    assert enqueued[0][0][kind_idx] == "request"
    _assert_no_store(response)


def test_path_estimate_denials_are_byte_equal_and_unlogged(livemap, monkeypatch):
    results = iter(
        [
            _loader_result(status="denied", points=[], auth=AUTH, as_of=1),
            _loader_result(status="denied", points=[], auth=None, as_of=2),
        ]
    )

    async def load(_flight_id):
        return next(results)

    monkeypatch.setattr(livemap, "_load_path_input", load)
    monkeypatch.setattr(livemap, "_est_cache", {})
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)
    client = TestClient(livemap.app)

    suppressed = client.get("/path/42/estimate")
    unknown = client.get("/path/42/estimate")

    assert suppressed.content == unknown.content
    assert enqueued == []
    _assert_no_store(suppressed)
    _assert_no_store(unknown)


def test_path_estimate_provisional_logs_request_only_without_cache(livemap, monkeypatch):
    _stub_loader(livemap, monkeypatch, _loader_result(status="provisional"))
    monkeypatch.setattr(
        livemap,
        "_fetch_od",
        lambda _fid: pytest.fail("provisional estimates must not fetch O/D"),
    )
    monkeypatch.setattr(
        livemap.est,
        "estimate",
        lambda *_args, **_kwargs: pytest.fail("provisional estimates must not run the estimator"),
    )
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    response = TestClient(livemap.app).get("/path/42/estimate")
    payload = response.json()

    assert payload["skips"] == [{"kind": "all", "reason": "provisional_input"}]
    assert payload["input_provisional"] is True
    assert len(enqueued) == 1 and len(enqueued[0]) == 1
    assert livemap._est_cache == {}
    _assert_no_store(response)


def test_path_estimate_settled_empty_logs_without_cache(livemap, monkeypatch):
    _stub_loader(livemap, monkeypatch, _loader_result(status="settled_empty", points=[]))
    monkeypatch.setattr(
        livemap,
        "_fetch_od",
        lambda _fid: pytest.fail("settled-empty estimates must not fetch O/D"),
    )
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    response = TestClient(livemap.app).get("/path/42/estimate")

    assert response.json()["skips"] == [{"kind": "all", "reason": "no_input"}]
    assert len(enqueued) == 1
    assert livemap._est_cache == {}
    _assert_no_store(response)


def test_path_estimate_od_failure_matches_denial_and_is_unlogged(livemap, monkeypatch):
    state = {"result": _loader_result()}

    async def load(_flight_id):
        return state["result"]

    def fail_od(_fid):
        raise RuntimeError("warehouse unavailable")

    monkeypatch.setattr(livemap, "_load_path_input", load)
    monkeypatch.setattr(livemap, "_fetch_od", fail_od)
    monkeypatch.setattr(livemap, "_est_cache", {})
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)
    client = TestClient(livemap.app)

    failed = client.get("/path/42/estimate")
    state["result"] = _loader_result(status="denied", points=[], auth=None)
    denied = client.get("/path/42/estimate")

    assert failed.content == denied.content
    assert enqueued == []
    _assert_no_store(failed)
    _assert_no_store(denied)


def test_path_estimate_cache_hit_reuses_payload_without_estimating(livemap, monkeypatch):
    calls = {"loader": 0, "estimate": 0}

    async def load(_flight_id):
        calls["loader"] += 1
        return _loader_result(as_of=1765500060 + calls["loader"])

    real_estimate = livemap.est.estimate

    def counting_estimate(points, od):
        calls["estimate"] += 1
        return real_estimate(points, od)

    monkeypatch.setattr(livemap, "_load_path_input", load)
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: livemap.est.OD())
    monkeypatch.setattr(livemap.est, "estimate", counting_estimate)
    monkeypatch.setattr(livemap, "_est_cache", {})
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)
    client = TestClient(livemap.app)

    first = client.get("/path/42/estimate")
    second = client.get("/path/42/estimate")

    assert calls["estimate"] == 1
    assert second.content == first.content
    assert len(enqueued) == 1
    _assert_no_store(first)
    _assert_no_store(second)


@pytest.mark.parametrize(("seed_id", "alias_id"), [("42", "042"), ("042", "42")])
def test_path_estimate_cache_hit_echoes_request_spelling(livemap, monkeypatch, seed_id, alias_id):
    # 42/042 share the canonical cache key — the wire must echo the caller's id, not the seeder's
    _stub_loader(livemap, monkeypatch, _loader_result())
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: livemap.est.OD())
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)
    client = TestClient(livemap.app)

    seeded = client.get(f"/path/{seed_id}/estimate").json()
    aliased = client.get(f"/path/{alias_id}/estimate").json()

    assert seeded["flight_id"] == seed_id
    assert aliased["flight_id"] == alias_id
    assert aliased["segments"] == seeded["segments"]   # same cached estimate, different echo


def test_path_estimate_cache_hit_rechecks_ladd(livemap, monkeypatch):
    state = {"result": _loader_result()}

    async def load(_flight_id):
        return state["result"]

    monkeypatch.setattr(livemap, "_load_path_input", load)
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: livemap.est.OD())
    monkeypatch.setattr(livemap, "_est_cache", {})
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)
    client = TestClient(livemap.app)
    seeded = client.get("/path/42/estimate")
    assert seeded.json()["segments"]

    monkeypatch.setattr(livemap, "_is_ladd_suppressed", lambda *_args, **_kwargs: True)
    suppressed = client.get("/path/42/estimate")
    state["result"] = _loader_result(status="denied", points=[], auth=AUTH)
    denied = client.get("/path/42/estimate")

    assert suppressed.content == denied.content
    assert len(enqueued) == 1
    _assert_no_store(seeded)
    _assert_no_store(suppressed)
    _assert_no_store(denied)


def test_est_flush_failure_drops_drained_group_count_from_health(livemap, monkeypatch):
    import asyncio

    queue = livemap.ess.LogQueue(max_groups=4)
    queue.put([("first",)])
    queue.put([("second",), ("second-segment",)])

    class _Client:
        def insert(self, *_args, **_kwargs):
            raise RuntimeError("write unavailable")

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_est_log_queue", queue)
    monkeypatch.setattr(livemap, "_ch_writer_client", _Client)
    monkeypatch.setattr(
        livemap,
        "_snapshot",
        {"server_ts": livemap.time.time(), "aircraft": []},
    )

    asyncio.run(livemap._flush_once())
    response = TestClient(livemap.app).get("/healthz")

    assert response.json()["est_log"] == {"queued": 0, "dropped": 2}


def test_est_flush_supplies_typed_context_and_closes_failed_client(livemap, monkeypatch):
    import asyncio

    queue = livemap.ess.LogQueue(max_groups=2)
    rows = [("request",), ("segment",)]
    queue.put(rows)
    calls = []

    class _Client:
        closed = False

        def insert(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("insert failed")

        def close(self):
            self.closed = True

    client = _Client()
    monkeypatch.setattr(livemap, "_est_log_queue", queue)
    monkeypatch.setattr(livemap, "_ch_writer_client", lambda: client)

    asyncio.run(livemap._flush_once())

    args, kwargs = calls[0]
    assert args == ("path_estimates", rows)
    assert kwargs["column_names"] == livemap.ess.INSERT_COLUMNS
    assert kwargs["column_type_names"] == livemap.ess.INSERT_TYPES
    assert client.closed is True


def test_flush_close_failure_after_landed_insert_is_not_a_drop(livemap, monkeypatch):
    # a cleanup error after a successful INSERT must not report landed estimates as lost
    import asyncio

    queue = livemap.ess.LogQueue(max_groups=2)
    queue.put([("request",)])
    landed = []

    class _Client:
        def insert(self, _table, inserted, **_kwargs):
            landed.extend(inserted)

        def close(self):
            raise RuntimeError("socket teardown hiccup")

    monkeypatch.setattr(livemap, "_est_log_queue", queue)
    monkeypatch.setattr(livemap, "_ch_writer_client", _Client)

    asyncio.run(livemap._flush_once())

    assert landed == [("request",)]
    assert queue.dropped == 0
    assert queue.groups == 0


def test_lifespan_final_flush_lands_queued_tail(livemap, monkeypatch):
    # a single bounded flush once stranded batches beyond the first — the bound must not cap the tail drain
    import asyncio

    queue = livemap.ess.LogQueue(max_groups=8)
    rows = [("request", 1)], [("request", 2)], [("request", 3)]
    for group in rows:
        queue.put(group)
    monkeypatch.setattr(livemap, "EST_FLUSH_MAX_ROWS", 1)
    landed = []

    class _Client:
        def insert(self, _table, inserted, **_kwargs):
            landed.extend(inserted)

        def close(self):
            pass

    async def idle():
        await asyncio.Event().wait()

    async def run_lifespan():
        async with livemap.lifespan(livemap.app):
            pass

    monkeypatch.setattr(livemap, "_est_log_queue", queue)
    monkeypatch.setattr(livemap, "_ch_writer_client", _Client)
    monkeypatch.setattr(livemap, "_poller", idle)
    monkeypatch.setattr(livemap, "_est_flusher", idle)

    asyncio.run(run_lifespan())

    assert landed == [row for group in rows for row in group]
    assert queue.groups == 0


def test_est_flush_missing_table_warns_once_and_counts_drops(
    livemap, monkeypatch, capsys
):
    import asyncio

    queue = livemap.ess.LogQueue(max_groups=2)

    class _Client:
        def insert(self, *_args, **_kwargs):
            raise RuntimeError(
                "Code: 60. DB::Exception: Table bronze.path_estimates doesn't exist. UNKNOWN_TABLE"
            )

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_est_log_queue", queue)
    monkeypatch.setattr(livemap, "_est_missing_table_warned", False)
    monkeypatch.setattr(livemap, "_ch_writer_client", _Client)

    queue.put([("first",)])
    asyncio.run(livemap._flush_once())
    first_warning = capsys.readouterr().out
    queue.put([("second",)])
    asyncio.run(livemap._flush_once())
    second_warning = capsys.readouterr().out

    assert livemap._est_missing_table_warned is True
    assert first_warning
    assert second_warning == ""
    assert queue.dropped == 2


def test_healthz_carries_est_log_queue_depth_and_drop_count(livemap, monkeypatch):
    queue = livemap.ess.LogQueue(max_groups=1)
    queue.put([("queued",)])
    queue.put([("dropped",)])
    monkeypatch.setattr(livemap, "_est_log_queue", queue)
    monkeypatch.setattr(
        livemap,
        "_snapshot",
        {"server_ts": livemap.time.time(), "aircraft": []},
    )

    response = TestClient(livemap.app).get("/healthz")

    assert response.status_code == 200
    assert response.json()["est_log"] == {"queued": 1, "dropped": 1}
