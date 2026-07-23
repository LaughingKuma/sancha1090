import asyncio
import importlib.util
import os
from pathlib import Path

import clickhouse_connect
from clickhouse_connect.driver.exceptions import DatabaseError
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "clickhouse")
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
SUPERSET_USER = os.environ.get("CH_SUPERSET_USER", "superset_ro")
SUPERSET_PASSWORD = os.environ.get("CH_SUPERSET_PASSWORD", "")
WRITER_PASSWORD = os.environ.get("CH_LIVEMAP_WRITER_PASSWORD", "")
TEST_METHOD_VERSION = "gc-dr-test"
TEST_FLIGHT_ID = 9_999_999_999_999_999_999
TEST_INPUT_AS_OF = 1_765_500_000

pytestmark = pytest.mark.skipif(not WRITER_PASSWORD, reason="deploy gate A not run")

es_spec = importlib.util.spec_from_file_location(
    "est_serving", REPO_ROOT / "livemap" / "est_serving.py"
)
es = importlib.util.module_from_spec(es_spec)
es_spec.loader.exec_module(es)


def _client(username, password):
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=username,
        password=password,
        database="bronze",
    )


def _superset_client():
    return _client(SUPERSET_USER, SUPERSET_PASSWORD)


def _writer_client():
    return _client("livemap_writer", WRITER_PASSWORD)


def _request_rows(serving=es):
    estimate_id = serving.new_estimate_id()
    rows = serving.build_log_rows(
        estimate_id,
        TEST_FLIGHT_ID,
        "",
        None,
        {
            "skips": [],
            "input_provisional": False,
            "segments": [],
            "input_as_of": TEST_INPUT_AS_OF,
        },
        [],
        serving.input_fingerprint([], serving.est.OD()),
        serving.utcnow(),
    )
    # §7 defines producer='serving' as the user-click population — test rows must not pollute it
    method_idx = serving.INSERT_COLUMNS.index("method_version")
    producer_idx = serving.INSERT_COLUMNS.index("producer")
    replacements = {method_idx: TEST_METHOD_VERSION, producer_idx: "test"}
    marked_rows = [
        tuple(replacements.get(idx, value) for idx, value in enumerate(row))
        for row in rows
    ]
    return estimate_id, marked_rows


def _insert(client, rows):
    client.insert(
        "bronze.path_estimates",
        rows,
        column_names=es.INSERT_COLUMNS,
        column_type_names=es.INSERT_TYPES,
    )


def _marked_count(client, estimate_id):
    result = client.query(
        """
        SELECT count()
        FROM bronze.path_estimates
        WHERE estimate_id = {estimate_id:UUID}
          AND method_version = {method_version:String}
        """,
        parameters={
            "estimate_id": str(estimate_id),
            "method_version": TEST_METHOD_VERSION,
        },
    )
    return result.result_rows[0][0]


def _assert_access_denied(exc):
    message = str(exc).lower()
    assert any(marker in message for marker in ("readonly", "access", "privilege")), message


@pytest.fixture(scope="module", autouse=True)
def require_path_estimates_table():
    client = _superset_client()
    try:
        exists = client.command("EXISTS TABLE bronze.path_estimates")
    finally:
        client.close()
    if not int(exists):
        pytest.skip("deploy gate A not run: bronze.path_estimates does not exist")


@pytest.fixture
def superset_ro():
    client = _superset_client()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def livemap_writer():
    client = _writer_client()
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def livemap():
    spec = importlib.util.spec_from_file_location("livemap_app_identity", REPO_ROOT / "livemap" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_superset_ro_insert_is_denied_without_landing(superset_ro):
    estimate_id, rows = _request_rows()

    with pytest.raises(DatabaseError) as raised:
        _insert(superset_ro, rows)

    assert _marked_count(superset_ro, estimate_id) == 0
    _assert_access_denied(raised.value)


def test_livemap_writer_can_insert_path_estimate(livemap_writer, superset_ro):
    estimate_id, rows = _request_rows()

    _insert(livemap_writer, rows)

    assert _marked_count(superset_ro, estimate_id) == 1


def test_livemap_writer_cannot_insert_adsb_states(livemap_writer):
    with pytest.raises(DatabaseError) as raised:
        livemap_writer.command(
            "INSERT INTO bronze.adsb_states (capture_ts) VALUES ('gc-dr-test-not-a-float')"
        )

    _assert_access_denied(raised.value)


def test_livemap_writer_cannot_select_path_estimates(livemap_writer):
    with pytest.raises(DatabaseError) as raised:
        livemap_writer.command("SELECT count() FROM bronze.path_estimates")

    _assert_access_denied(raised.value)


def test_real_flusher_drains_and_inserts(livemap, monkeypatch, superset_ro):
    monkeypatch.setattr(livemap, "CH_HOST", CLICKHOUSE_HOST)
    monkeypatch.setattr(livemap, "CH_PORT", CLICKHOUSE_PORT)
    monkeypatch.setattr(livemap, "CH_WRITER_USER", "livemap_writer")
    monkeypatch.setattr(livemap, "CH_WRITER_PASSWORD", WRITER_PASSWORD)
    monkeypatch.setattr(livemap.ess, "METHOD_VERSION", TEST_METHOD_VERSION)
    estimate_id, rows = _request_rows(livemap.ess)

    livemap._est_log_queue.put(rows)
    asyncio.run(livemap._flush_once())

    assert _marked_count(superset_ro, estimate_id) == len(rows)
