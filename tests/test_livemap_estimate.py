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
    assert "x-estimate-id" not in suppressed.headers
    assert "x-estimate-id" not in unknown.headers
    _assert_no_store(suppressed)
    _assert_no_store(unknown)


def test_path_estimate_provisional_computes_serves_and_logs_never_caches(livemap, monkeypatch):
    _stub_loader(livemap, monkeypatch, _loader_result(status="provisional"))
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: livemap.est.OD())
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    response = TestClient(livemap.app).get("/path/42/estimate")
    payload = response.json()

    # POINTS end airborne with valid motion and a NULL dest -> the estimator's capped DR fires
    assert payload["input_provisional"] is True
    assert [s["kind"] for s in payload["segments"]] == ["dr"]
    assert len(enqueued) == 1
    assert len(enqueued[0]) == 1 + len(payload["segments"])
    prov_idx = livemap.ess.INSERT_COLUMNS.index("input_provisional")
    assert enqueued[0][0][prov_idx] == 1
    eid_idx = livemap.ess.INSERT_COLUMNS.index("estimate_id")
    assert response.headers["x-estimate-id"] == str(enqueued[0][0][eid_idx])  # causal key (rev 7)
    assert livemap._est_cache == {}
    _assert_no_store(response)


def test_path_estimate_provisional_recomputes_every_click(livemap, monkeypatch):
    # never cached: two identical provisional clicks pay the estimator twice (rung-2 invariant)
    calls = {"estimate": 0}
    real_estimate = livemap.est.estimate

    def counting_estimate(points, od):
        calls["estimate"] += 1
        return real_estimate(points, od)

    _stub_loader(livemap, monkeypatch, _loader_result(status="provisional"))
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: livemap.est.OD())
    monkeypatch.setattr(livemap.est, "estimate", counting_estimate)
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)
    client = TestClient(livemap.app)

    client.get("/path/42/estimate")
    client.get("/path/42/estimate")

    assert calls["estimate"] == 2
    assert livemap._est_cache == {}


def test_path_estimate_provisional_od_failure_matches_denial_and_is_unlogged(livemap, monkeypatch):
    state = {"result": _loader_result(status="provisional")}

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
    assert "x-estimate-id" not in failed.headers
    assert "x-estimate-id" not in denied.headers
    _assert_no_store(failed)
    _assert_no_store(denied)


def test_path_estimate_provisional_all_skipped_logs_without_header(livemap, monkeypatch):
    # A computed non-result is logged truthfully but must remain header-identical to denials.
    _stub_loader(livemap, monkeypatch, _loader_result(status="provisional"))
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: livemap.est.OD())
    monkeypatch.setattr(
        livemap.est,
        "estimate",
        lambda *_args, **_kwargs: livemap.est.EstimateResult(
            [], [{"kind": "dr", "reason": "invalid_motion"}], []
        ),
    )
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    response = TestClient(livemap.app).get("/path/42/estimate")
    payload = response.json()

    assert payload["segments"] == []
    assert payload["skips"] == [{"kind": "dr", "reason": "invalid_motion"}]
    assert len(enqueued) == 1
    skips_idx = livemap.ess.INSERT_COLUMNS.index("skips")
    assert enqueued[0][0][skips_idx] == [("dr", "invalid_motion")]
    assert "x-estimate-id" not in response.headers
    _assert_no_store(response)


def test_estimate_id_header_stays_off_settled_cache_miss_and_hit(livemap, monkeypatch):
    # Gate C needs causal IDs only for never-cached PR-3 arms; settled cache state stays private.
    _stub_loader(livemap, monkeypatch, _loader_result())
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: livemap.est.OD())
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)
    client = TestClient(livemap.app)

    fresh = client.get("/path/42/estimate")
    cached = client.get("/path/42/estimate")

    assert len(enqueued) == 1
    assert "x-estimate-id" not in fresh.headers
    assert "x-estimate-id" not in cached.headers


def test_path_estimate_provisional_fingerprints_with_real_od(livemap, monkeypatch):
    # §7 canonical fingerprint: the computed provisional arm hashes the REAL O/D, not the sentinel
    _stub_loader(livemap, monkeypatch, _loader_result(status="provisional"))
    od = livemap.est.OD(dest=livemap.est.Endpoint(34.78, 135.44, "vrs_routes", "unanimous"))
    monkeypatch.setattr(livemap, "_fetch_od", lambda _fid: od)
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    TestClient(livemap.app).get("/path/42/estimate")

    fp_idx = livemap.ess.INSERT_COLUMNS.index("input_fingerprint")
    assert enqueued[0][0][fp_idx] == livemap.ess.input_fingerprint(POINTS, od)
    assert enqueued[0][0][fp_idx] != livemap.ess.input_fingerprint(POINTS, livemap.est.OD())


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
    assert "x-estimate-id" not in response.headers
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
    assert "x-estimate-id" not in failed.headers
    assert "x-estimate-id" not in denied.headers
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

    assert response.json()["est_log"] == {
        "queued": 0,
        "dropped": 2,
        "accepted": 2,
        "written": 0,
    }


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
    assert queue.written == 1
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
    assert response.json()["est_log"] == {
        "queued": 1,
        "dropped": 1,
        "accepted": 1,
        "written": 0,
    }


LIVE_ROW = {"hex": "abc123", "flight": "ANA1", "lat": 35.0, "lon": 139.5,
            "alt_baro": "35000", "gs": 450.0, "track": 90.0}


def _live_snapshot(livemap, monkeypatch, row=None, age=5.0, snap_age=0.0):
    now = livemap.time.time()
    aircraft = [] if row is None else [{**LIVE_ROW, "capture_ts": now - age, **row}]
    monkeypatch.setattr(livemap, "_snapshot", {"server_ts": now - snap_age, "aircraft": aircraft})


def test_estimate_live_serves_single_dr_and_logs_with_anchor(livemap, monkeypatch):
    _live_snapshot(livemap, monkeypatch, row={})
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    response = TestClient(livemap.app).get("/estimate/live/abc123")
    payload = response.json()

    assert payload["flight_id"] is None and payload["icao24"] == "abc123"
    assert [s["kind"] for s in payload["segments"]] == ["dr"]
    assert payload["skips"] == []
    idx = livemap.ess.INSERT_COLUMNS.index
    assert len(enqueued) == 1 and len(enqueued[0]) == 2
    assert enqueued[0][0][idx("flight_id")] is None
    assert enqueued[0][0][idx("icao24")] == "abc123"
    assert enqueued[0][0][idx("anchor_ts")] is not None
    assert response.headers["x-estimate-id"] == str(enqueued[0][0][idx("estimate_id")])
    _assert_no_store(response)


def test_estimate_live_all_denials_are_byte_equal_and_pregate_unlogged(livemap, monkeypatch):
    # design §5: stale, on-ground, invalid, unknown, LADD, suppress-None -> ONE empty shape.
    # Ruling 1: only the post-gate computations (on-ground, invalid motion) leave log rows.
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)
    client = TestClient(livemap.app)
    responses = {}

    _live_snapshot(livemap, monkeypatch)                                   # not in snapshot
    responses["unknown"] = client.get("/estimate/live/abc123")
    _live_snapshot(livemap, monkeypatch, row={}, snap_age=livemap.EST_LIVE_SNAP_FRESH_S + 1)
    responses["stale_snap"] = client.get("/estimate/live/abc123")
    _live_snapshot(livemap, monkeypatch, row={}, age=livemap.EST_LIVE_MAX_AGE_S + 1)
    responses["aged_fix"] = client.get("/estimate/live/abc123")
    _live_snapshot(livemap, monkeypatch, row={})
    monkeypatch.setattr(livemap, "_ladd_suppress",
                        {"hex": frozenset({"abc123"}), "callsign": frozenset()})
    responses["ladd"] = client.get("/estimate/live/abc123")
    monkeypatch.setattr(livemap, "_ladd_suppress", None)
    responses["suppress_none"] = client.get("/estimate/live/abc123")
    monkeypatch.setattr(livemap, "_ladd_suppress", livemap._EMPTY_SUPPRESS)
    assert enqueued == []                                                  # pre-gate: never logged

    _live_snapshot(livemap, monkeypatch, row={"alt_baro": "ground"})
    responses["on_ground"] = client.get("/estimate/live/abc123")
    _live_snapshot(livemap, monkeypatch, row={"gs": None})
    responses["invalid_motion"] = client.get("/estimate/live/abc123")

    assert len({r.content for r in responses.values()}) == 1               # byte-uniform wire
    # r1 review: a class-specific header (or a branch losing no-store) must fail here too —
    # the contract is byte-AND-header uniformity, not body equality alone
    assert len({tuple(sorted(r.headers.items())) for r in responses.values()}) == 1
    for r in responses.values():
        _assert_no_store(r)
    assert all("x-estimate-id" not in r.headers for r in responses.values())
    assert len(enqueued) == 2                                              # post-gate: truthful log
    skips_idx = livemap.ess.INSERT_COLUMNS.index("skips")
    assert enqueued[0][0][skips_idx] == [("dr", "on_ground_edge")]
    assert enqueued[1][0][skips_idx] == [("dr", "invalid_motion")]
    assert all(len(g) == 1 for g in enqueued)                              # request row only


def test_estimate_live_belt_suppressed_hex_denied_unlogged(livemap, monkeypatch):
    now = livemap.time.time()
    _live_snapshot(livemap, monkeypatch, row={})
    monkeypatch.setattr(livemap, "_mv_ladd_hexes", {"abc123": now})
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    response = TestClient(livemap.app).get("/estimate/live/abc123")

    assert response.json()["segments"] == []
    assert enqueued == []
    assert "x-estimate-id" not in response.headers
    _assert_no_store(response)


def test_estimate_live_malformed_hex_is_pregate_denied_even_when_present(livemap, monkeypatch):
    # rev 9: the RW MV can carry a raw malformed producer hex — matching it must never
    # compute, log a malformed h: subject, or earn a header (byte-shape equals every denial)
    now = livemap.time.time()
    monkeypatch.setattr(livemap, "_snapshot",
                        {"server_ts": now,
                         "aircraft": [{**LIVE_ROW, "hex": "zz!bad", "capture_ts": now - 5.0}]})
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    response = TestClient(livemap.app).get("/estimate/live/zz!bad")

    assert response.json()["segments"] == []
    assert response.json()["skips"] == [{"kind": "all", "reason": "no_input"}]
    assert enqueued == []
    assert "x-estimate-id" not in response.headers
    _assert_no_store(response)


def test_estimate_live_never_cached_recomputes_each_click(livemap, monkeypatch):
    calls = {"estimate": 0}
    real_estimate = livemap.est.estimate

    def counting_estimate(points, od):
        calls["estimate"] += 1
        return real_estimate(points, od)

    _live_snapshot(livemap, monkeypatch, row={})
    monkeypatch.setattr(livemap.est, "estimate", counting_estimate)
    monkeypatch.setattr(livemap, "_est_cache", {})  # pinned: module-state independence (rev 9)
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)
    client = TestClient(livemap.app)

    client.get("/estimate/live/abc123")
    client.get("/estimate/live/abc123")

    assert calls["estimate"] == 2
    assert livemap._est_cache == {}


def test_estimate_live_normalizes_hex_and_echoes_caller_spelling(livemap, monkeypatch):
    _live_snapshot(livemap, monkeypatch, row={})
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)

    payload = TestClient(livemap.app).get("/estimate/live/ABC123").json()

    assert payload["icao24"] == "ABC123"          # wire echoes the caller, like /track
    assert payload["segments"]
    icao_idx = livemap.ess.INSERT_COLUMNS.index("icao24")
    assert enqueued[0][0][icao_idx] == "abc123"   # log keys the mart's lowercase form


def test_estimate_live_as_of_is_snapshot_server_ts(livemap, monkeypatch):
    now = livemap.time.time()
    monkeypatch.setattr(livemap, "_snapshot",
                        {"server_ts": now - 2.0, "aircraft": [{**LIVE_ROW, "capture_ts": now - 3.0}]})
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)

    payload = TestClient(livemap.app).get("/estimate/live/abc123").json()

    assert payload["input_as_of"] == int(now - 2.0)


def test_estimate_live_future_timestamps_are_denied_and_unlogged(livemap, monkeypatch):
    # rev 2: a clock an hour ahead is invalid data, not freshness — both gates fail closed
    enqueued = []
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", enqueued.append)
    client = TestClient(livemap.app)

    _live_snapshot(livemap, monkeypatch, row={}, snap_age=-3600.0)
    future_snap = client.get("/estimate/live/abc123")
    _live_snapshot(livemap, monkeypatch, row={}, age=-3600.0)
    future_fix = client.get("/estimate/live/abc123")

    assert future_snap.json()["segments"] == []
    assert future_snap.content == future_fix.content   # same uniform empty as every denial
    assert "x-estimate-id" not in future_snap.headers
    assert "x-estimate-id" not in future_fix.headers
    assert enqueued == []


def test_estimate_live_small_future_skew_is_tolerated(livemap, monkeypatch):
    # sub-skew negative ages are normal cross-machine jitter, not staleness
    _live_snapshot(livemap, monkeypatch, row={}, age=-1.0, snap_age=-1.0)
    monkeypatch.setattr(livemap, "_enqueue_estimate_log", lambda _rows: None)

    payload = TestClient(livemap.app).get("/estimate/live/abc123").json()

    assert [s["kind"] for s in payload["segments"]] == ["dr"]


def test_flush_success_increments_written_by_group_count(livemap, monkeypatch):
    import asyncio

    queue = livemap.ess.LogQueue(max_groups=4)
    queue.put([("r1",)])
    queue.put([("r2",), ("s2",)])

    class _Client:
        def insert(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_est_log_queue", queue)
    monkeypatch.setattr(livemap, "_ch_writer_client", _Client)

    asyncio.run(livemap._flush_once())

    assert queue.written == 2 and queue.dropped == 0 and queue.accepted == 2


def test_flush_failure_leaves_written_untouched(livemap, monkeypatch):
    import asyncio

    queue = livemap.ess.LogQueue(max_groups=4)
    queue.put([("r1",)])

    class _Client:
        def insert(self, *_args, **_kwargs):
            raise RuntimeError("write unavailable")

        def close(self):
            pass

    monkeypatch.setattr(livemap, "_est_log_queue", queue)
    monkeypatch.setattr(livemap, "_ch_writer_client", _Client)

    asyncio.run(livemap._flush_once())

    assert queue.written == 0 and queue.dropped == 1


def test_healthz_est_log_carries_all_four_counters(livemap, monkeypatch):
    queue = livemap.ess.LogQueue(max_groups=4)
    queue.put([("q",)])
    queue.record_written(3)
    monkeypatch.setattr(livemap, "_est_log_queue", queue)
    monkeypatch.setattr(livemap, "_snapshot", {"server_ts": livemap.time.time(), "aircraft": []})

    body = TestClient(livemap.app).get("/healthz").json()

    assert body["est_log"] == {"queued": 1, "dropped": 0, "accepted": 1, "written": 3}
