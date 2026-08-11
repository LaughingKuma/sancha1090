import os
import time

import pytest
from conftest import load_livemap_module

lm = load_livemap_module("app.py")

try:
    import clickhouse_connect
    from clickhouse_connect.driver import exceptions as ch_exc
except ImportError:  # pragma: no cover - host env without the driver
    clickhouse_connect = None


def test_est_route_query_executes_on_live_clickhouse():
    # r5 HIGH regression guard: executes the LITERAL query server-side, so a query ClickHouse
    # rejects (e.g. bound-param LIKE = ILLEGAL_COLUMN) can never again hide behind mocks.
    if clickhouse_connect is None:
        pytest.skip("clickhouse-connect unavailable")
    try:
        client = clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
    except (ch_exc.OperationalError, OSError) as exc:
        # ONLY connectivity skips; auth/config/server errors must FAIL the guard, not disarm it
        pytest.skip(f"live ClickHouse unreachable: {exc}")
    now = int(time.time())
    try:
        res = client.query(
            lm.EST_ROUTE_QUERY,
            parameters={"callsign": "ZZZZ99", "start": now - 43200, "end": now,
                        "origin": "ZZZZ", "dest": "ZZZZ"},
            settings={"max_execution_time": lm.EST_ROUTE_TIMEOUT_S},
        )
    finally:
        client.close()
    # current-window params so real partitions are read (epoch params vacuously prune everything)
    assert res.result_rows == []


def test_plan_class_ranking_executes_with_competing_rows():
    # r8/r9 regressions, executed over synthetic competing rows through the LITERAL query:
    # newer tokenless full plan beats older coords; './.' never beats a full plan; wrong-leg veto.
    if clickhouse_connect is None:
        pytest.skip("clickhouse-connect unavailable")
    try:
        client = clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
    except (ch_exc.OperationalError, OSError) as exc:
        pytest.skip(f"live ClickHouse unreachable: {exc}")

    structure = ("acid Nullable(String), msg_type Nullable(String), "
                 "msg_timestamp Nullable(DateTime64(6)), "
                 "filed_departure_time Nullable(DateTime64(6)), swim_date Date, "
                 "raw_xml Nullable(String), dep_point Nullable(String), "
                 "arr_point Nullable(String), dep_point_kind Nullable(String), "
                 "arr_point_kind Nullable(String), _dedup_fp UInt64")

    def rows_sql(rows):
        vals = ", ".join(rows)
        return lm.EST_ROUTE_QUERY.replace(
            "FROM bronze.swim_flightdata",
            f"FROM (SELECT * FROM VALUES('{structure}', {vals}))",
        )

    old_coords = ("('TESTX1', 'flightPlanInformation', toDateTime64(1785010000, 6), "
                  "toDateTime64(1785002000, 6), toDate('2026-07-24'), "
                  "'<x legacyFormat=\"KAAA..5000N/15000W..RJBB\"/>', 'AAA', 'RJBB', 'airport', 'airport', 1)")
    new_no_coords = ("('TESTX1', 'flightPlanInformation', toDateTime64(1785020000, 6), "
                     "toDateTime64(1785002000, 6), toDate('2026-07-24'), "
                     "'<x legacyFormat=\"KAAA..FIXA.R220.FIXB..RJBB\"/>', 'AAA', 'RJBB', 'airport', 'airport', 2)")
    new_truncated = ("('TESTX1', 'flightPlanAmendmentInformation', toDateTime64(1785030000, 6), "
                     "toDateTime64(1785002000, 6), toDate('2026-07-24'), "
                     "'<x legacyFormat=\"KAAA./.POS..5000N/15000W..RJBB\"/>', 'AAA', 'RJBB', 'airport', 'airport', 3)")
    params = {"callsign": "TESTX1", "start": 1785003000, "end": 1785040000,
              "origin": "KAAA", "dest": "RJBB"}
    wrong_leg = ("('TESTX1', 'flightPlanInformation', toDateTime64(1785025000, 6), "
                 "toDateTime64(1785002000, 6), toDate('2026-07-24'), "
                 "'<x legacyFormat=\"KAAA..6000N/16000W..RKSI\"/>', 'AAA', 'RKSI', "
                 "'airport', 'airport', 4)")
    try:
        reroute = client.query(rows_sql([old_coords, new_no_coords]), parameters=params)
        amendment = client.query(rows_sql([old_coords, new_truncated]), parameters=params)
        veto = client.query(rows_sql([old_coords, wrong_leg]), parameters=params)
    finally:
        client.close()
    assert reroute.result_rows[0][0] == "KAAA..FIXA.R220.FIXB..RJBB"
    assert amendment.result_rows[0][0] == "KAAA..5000N/15000W..RJBB"
    # r9: a newer same-callsign plan whose known arrival CONFLICTS with the flight's dest loses
    assert veto.result_rows[0][0] == "KAAA..5000N/15000W..RJBB"
