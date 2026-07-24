import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
es_spec = importlib.util.spec_from_file_location(
    "est_serving", REPO_ROOT / "livemap" / "est_serving.py"
)
es = importlib.util.module_from_spec(es_spec)
es_spec.loader.exec_module(es)
est_spec = importlib.util.spec_from_file_location(
    "estimator", REPO_ROOT / "livemap" / "estimator.py"
)
est = importlib.util.module_from_spec(est_spec)
est_spec.loader.exec_module(est)


def test_path_estimates_ddl_shape():
    sql = (REPO_ROOT / "clickhouse" / "sql" / "01_bronze.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS bronze.path_estimates" in sql
    assert "subject_key" in sql and "MATERIALIZED" in sql
    assert "PARTITION BY toYYYYMM(computed_at)" in sql
    assert "ORDER BY (subject_key, computed_at, estimate_id, seg_idx)" in sql
    assert "TTL toDateTime(computed_at) + INTERVAL 24 MONTH" in sql
    assert "wind_samples" in sql and "meta_json" in sql


def test_livemap_writer_xml_grants_are_exact():
    xml = (REPO_ROOT / "clickhouse" / "users.d" / "livemap_writer.xml").read_text()
    assert '<password from_env="CH_LIVEMAP_WRITER_PASSWORD"/>' in xml
    assert "<grants>" in xml
    assert "GRANT INSERT ON bronze.path_estimates" in xml
    # the explicit block must be the ONLY grant — broad legacy privileges must not leak in
    assert xml.count("<query>") == 1


def test_writer_env_reaches_both_sidecars_and_env_example():
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    # The writer identity must reach both serving sidecars; a private-only secret would silently
    # 500 the public estimate log.
    assert compose.count("LIVEMAP_CH_WRITER_PASSWORD") >= 2
    assert "CH_LIVEMAP_WRITER_PASSWORD" in (REPO_ROOT / ".env.example").read_text()


def test_uncertainty_bands_cover_every_serving_bin():
    for b in ("gap_15_60m", "gap_60_180m", "gap_180m_plus", "dest_ext", "origin_ext", "dr"):
        assert b in es.UNCERTAINTY_BANDS
    # a single holdout observation cannot calibrate the >180m band — the last calibrated values serve as a floor
    floor_band = es.UNCERTAINTY_BANDS["gap_180m_plus"]
    calibrated_band = es.UNCERTAINTY_BANDS["gap_60_180m"]
    assert floor_band["p50_km"] == calibrated_band["p50_km"]
    assert floor_band["p90_km"] == calibrated_band["p90_km"]
    assert floor_band["floor"] is True


def test_gap_180m_plus_segment_serves_floor_marker():
    class Segment:
        meta = {
            "bin": "gap_180m_plus",
            "gs_entry_kt": 430.0,
            "gs_exit_kt": 440.0,
            "capped": False,
            "confidence": "low",
        }

    uncertainty = es._segment_meta(Segment())["uncertainty"]
    assert uncertainty["floor"] is True


def test_fingerprint_deterministic_and_od_sensitive():
    pts = [(0, 35.0, 139.0, None, 0, 450.0, 90.0, "adsb")]
    od1, od2 = est.OD(), est.OD(dest=est.Endpoint(35.0, 152.0))
    assert es.input_fingerprint(pts, od1) == es.input_fingerprint(pts, od1)
    assert es.input_fingerprint(pts, od1) != es.input_fingerprint(pts, od2)
    assert 0 <= es.input_fingerprint(pts, od1) < 2**64


def test_build_response_meta_shape():
    pts = [(t * 60, 35.0, 139.0 + 0.05 * t, 35000.0, 0, 450.0, 90.0, "adsb") for t in range(10)]
    r = est.estimate(pts, est.OD())
    payload = es.build_response("42", r, False, 1765500000)
    assert payload["method_version"] == "gc-dr-1" and payload["wind_source"] == "none"
    assert payload["input_as_of"] == 1765500000 and payload["input_provisional"] is False
    seg = payload["segments"][0]
    assert seg["meta"]["wind"] == {"source": "none", "coverage": 0.0}
    assert set(seg["meta"]["uncertainty"]) == {"p50_km", "p90_km", "bin", "floor"}
    assert seg["meta"]["tas_carried_kt"] is None


def test_log_rows_request_row_always_first():
    pts = [(t * 60, 35.0, 139.0 + 0.05 * t, 35000.0, 0, 450.0, 90.0, "adsb") for t in range(10)]
    r = est.estimate(pts, est.OD())
    payload = es.build_response("42", r, False, 1765500000)
    rows = es.build_log_rows(es.new_estimate_id(), 42, "abc123", r, payload, pts,
                             es.input_fingerprint(pts, est.OD()), es.utcnow())
    assert rows[0][es.INSERT_COLUMNS.index("kind")] == "request"
    assert rows[0][es.INSERT_COLUMNS.index("seg_idx")] == 0
    assert rows[0][es.INSERT_COLUMNS.index("icao24")] == ""   # §7: fid-keyed rows log ''
    assert len(rows) == 1 + len(payload["segments"])
    assert len(es.INSERT_COLUMNS) == len(es.INSERT_TYPES) == 31
    assert "subject_key" not in es.INSERT_COLUMNS


def test_log_rows_result_none_is_request_row_only():
    payload = {"skips": [{"kind": "all", "reason": "provisional_input"}], "input_provisional": True,
               "segments": [], "method_version": es.METHOD_VERSION, "wind_source": "none",
               "input_as_of": 1765500000}
    rows = es.build_log_rows(es.new_estimate_id(), 42, "abc123", None, payload, [],
                             es.input_fingerprint([], est.OD()), es.utcnow())
    assert len(rows) == 1
    assert rows[0][es.INSERT_COLUMNS.index("kind")] == "request"
    assert rows[0][es.INSERT_COLUMNS.index("input_provisional")] == 1


def test_dockerfile_bakes_estimator_modules():
    df = (REPO_ROOT / "livemap" / "Dockerfile").read_text()
    assert "COPY app.py estimator.py est_serving.py ." in df


def test_log_queue_full_drops_arriving_group_and_preserves_order():
    queue = es.LogQueue(max_groups=2)
    first = [("first", 1)]
    second = [("second", 1), ("second", 2)]

    queue.put(first)
    queue.put(second)
    queue.put([("arriving", 1)])

    assert queue.groups == 2
    assert queue.dropped == 1
    rows, ngroups = queue.drain(100)
    assert rows == first + second
    assert ngroups == 2


def test_log_queue_concurrent_drains_never_lose_or_double_count_groups():
    # the shutdown tail drain can overlap a cancelled-but-running writer thread — drains must be atomic
    import threading

    queue = es.LogQueue(max_groups=512)
    for i in range(200):
        queue.put([("g", i)])
    seen, seen_lock = [], threading.Lock()

    def worker():
        while True:
            rows, _n = queue.drain(1)
            if not rows:
                return
            with seen_lock:
                seen.extend(rows)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(r[1] for r in seen) == list(range(200))
    assert queue.dropped == 0 and queue.groups == 0


def test_log_queue_drain_never_splits_groups_and_allows_one_oversize_group():
    queue = es.LogQueue(max_groups=3)
    first = [("first", 1), ("first", 2)]
    oversize = [("second", 1), ("second", 2), ("second", 3)]
    queue.put(first)
    queue.put(oversize)

    rows, ngroups = queue.drain(4)
    assert rows == first
    assert ngroups == 1
    assert queue.groups == 1

    rows, ngroups = queue.drain(2)
    assert rows == oversize
    assert ngroups == 1
    assert queue.groups == 0


LIVE_ANCHOR = [(1765500000.0, 35.0, 139.0, 35000.0, 0, 450.0, 90.0, "live")]


def test_build_live_response_serves_single_dr_and_filters_od_noise():
    r = est.estimate(LIVE_ANCHOR, est.OD())
    payload = es.build_live_response("abc123", r, 1765500005)
    assert payload["flight_id"] is None and payload["icao24"] == "abc123"
    assert payload["input_provisional"] is False and payload["input_as_of"] == 1765500005
    assert [s["kind"] for s in payload["segments"]] == ["dr"]
    # the synthesized OD() always yields origin_ext/missing_endpoint — noise, never served
    assert payload["skips"] == []
    assert payload["method_version"] == es.METHOD_VERSION and payload["wind_source"] == "none"


def test_build_live_response_keeps_dr_and_all_skips_only():
    r = est.EstimateResult([], [{"kind": "origin_ext", "reason": "missing_endpoint"},
                                {"kind": "dr", "reason": "invalid_motion"}], [])
    assert es.build_live_response("abc123", r, 7)["skips"] == [
        {"kind": "dr", "reason": "invalid_motion"}]


def test_log_rows_live_carry_anchor_ts_null_fid_and_hex():
    r = est.estimate(LIVE_ANCHOR, est.OD())
    payload = es.build_live_response("abc123", r, 1765500005)
    rows = es.build_log_rows(es.new_estimate_id(), None, "abc123", r, payload,
                             LIVE_ANCHOR, es.input_fingerprint(LIVE_ANCHOR, est.OD()),
                             es.utcnow(), anchor_ts=1765500000.0)
    idx = es.INSERT_COLUMNS.index
    assert rows[0][idx("flight_id")] is None
    assert rows[0][idx("icao24")] == "abc123"          # §7: hex identifies live rows
    assert rows[0][idx("anchor_ts")] == es._as_utc_datetime(1765500000.0)
    assert rows[0][idx("input_provisional")] == 0
    assert len(rows) == 2 and rows[1][idx("kind")] == "dr"
    assert rows[1][idx("anchor_ts")] == es._as_utc_datetime(1765500000.0)


def test_log_rows_anchor_ts_defaults_none_for_fid_estimates():
    pts = [(t * 60, 35.0, 139.0 + 0.05 * t, 35000.0, 0, 450.0, 90.0, "adsb") for t in range(10)]
    r = est.estimate(pts, est.OD())
    payload = es.build_response("42", r, False, 1765500000)
    rows = es.build_log_rows(es.new_estimate_id(), 42, "abc123", r, payload, pts,
                             es.input_fingerprint(pts, est.OD()), es.utcnow())
    assert rows[0][es.INSERT_COLUMNS.index("anchor_ts")] is None


def test_log_queue_counts_accepted_and_written_monotonically():
    # secondary writer-liveness evidence (rev 5/7): accepted counts successful puts only,
    # written counts only CH-confirmed inserts — queued/dropped alone prove nothing
    queue = es.LogQueue(max_groups=1)
    queue.put([("kept",)])
    queue.put([("dropped",)])
    assert queue.accepted == 1 and queue.dropped == 1 and queue.written == 0
    queue.record_written(1)
    assert queue.written == 1


def test_log_segment_rows_mirror_the_response_losslessly():
    # §7 lossless-record proof lives HERE (rev 6): the gate only proves cardinality/kinds
    # landing — points transpose, meta_json, and skips equivalence are unit-tested
    r = est.estimate(LIVE_ANCHOR, est.OD())
    payload = es.build_live_response("abc123", r, 1765500005)
    rows = es.build_log_rows(es.new_estimate_id(), None, "abc123", r, payload,
                             LIVE_ANCHOR, es.input_fingerprint(LIVE_ANCHOR, est.OD()),
                             es.utcnow(), anchor_ts=1765500000.0)
    idx = es.INSERT_COLUMNS.index
    seg = payload["segments"][0]
    assert rows[1][idx("points")] == [(p[2], p[1], p[0], p[3]) for p in seg["points"]]
    assert rows[1][idx("meta_json")] == es._canonical_json(seg["meta"])
    assert rows[0][idx("skips")] == [(s["kind"], s["reason"]) for s in payload["skips"]]
    assert rows[1][idx("seg_idx")] == 1


_CONF = {"endpoint_source": None, "endpoint_agreement": None, "times_low_confidence": False}


def test_log_rows_mirror_multi_segment_responses_in_order():
    # rev 7: the one-segment live case cannot catch later-row corruption, swapped indices,
    # or a lost non-empty skip list — a deterministic 2-kind result pins every row in order
    seg_a = est.Segment("gap", [[139.0, 35.0, 100, 30000.0], [139.5, 35.2, 160, 31000.0]],
                        {"bin": "gap_15_60m", "gs_entry_kt": 440.0, "gs_exit_kt": 450.0,
                         "capped": False, "confidence": _CONF})
    seg_b = est.Segment("dr", [[139.5, 35.2, 160, 31000.0], [139.6, 35.3, 220, 31000.0]],
                        {"bin": "dr", "gs_entry_kt": 450.0, "gs_exit_kt": 450.0,
                         "capped": True, "confidence": _CONF})
    r = est.EstimateResult([seg_a, seg_b],
                           [{"kind": "origin_ext", "reason": "missing_endpoint"}], [])
    payload = es.build_response("42", r, True, 1765500000)
    pts = [(90, 35.0, 139.0, None, 0, 440.0, 90.0, "adsb")]
    rows = es.build_log_rows(es.new_estimate_id(), 42, "", r, payload, pts,
                             es.input_fingerprint(pts, est.OD()), es.utcnow())
    idx = es.INSERT_COLUMNS.index
    assert len(rows) == 1 + len(payload["segments"])
    assert rows[0][idx("skips")] == [("origin_ext", "missing_endpoint")]
    for n, seg in enumerate(payload["segments"], start=1):
        assert rows[n][idx("seg_idx")] == n                       # contiguous, in response order
        assert rows[n][idx("kind")] == seg["kind"]
        assert rows[n][idx("points")] == [(p[2], p[1], p[0], p[3]) for p in seg["points"]]
        assert rows[n][idx("meta_json")] == es._canonical_json(seg["meta"])
