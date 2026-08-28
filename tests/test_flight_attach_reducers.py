from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODEL = REPO / "dbt" / "sancha1090" / "models" / "silver" / "int_flight_attached_votes.sql"


def _tail() -> str:
    # anchor_pick -> votes -> final select with comments stripped: the two reducers, nothing that
    # needs a ref().
    src = MODEL.read_text()
    return re.sub(r"--[^\n]*", "", src[src.index("anchor_pick as ("):])


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def test_reducer_keys_are_pinned_verbatim():
    # Whitespace-free pins of both argMin keys: tie order and NULL-last encoding are what keep ambiguous votes
    # stable (13.7k adsblol / 3.9k opensky_states windows had >1 valid spine on 2026-08-21).
    tail = _squash(_tail())
    assert (
        "argMin(tuple(flight_id,source_rank,origin_icao,dest_icao,origin_gated,dest_gated,overlap_s),"
        "tuple(-overlap_s,anchor_rank,flight_id,isNull(origin_icao),ifNull(origin_icao,''),"
        "isNull(dest_icao),ifNull(dest_icao,'')))aspicked"
    ) in tail
    assert (
        "argMin(tuple(picked.2,picked.3,picked.4),tuple(-multiIf(source='opensky_flights',"
        "toInt8(if(picked.3isnotnull,1,0)+if(picked.4isnotnull,1,0)),toInt8(0)),-picked.7,win_start,"
        "isNull(picked.3),ifNull(picked.3,''),isNull(picked.4),ifNull(picked.4,'')))asvote"
    ) in tail
    assert "max(picked.5)assrc_origin_gated,max(picked.6)assrc_dest_gated" in tail


@pytest.fixture()
def ch():
    # Same connect/skip contract as test_ch_migration_integration: skip without CH, fail when CI
    # says it must run.
    try:
        import clickhouse_connect
        c = clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
        c.command("SELECT 1")
    except Exception as e:
        if os.environ.get("CH_INTEGRATION_REQUIRED") == "1":
            pytest.fail(f"ClickHouse required but not reachable: {e!r}")
        pytest.skip(f"ClickHouse not reachable: {e!r}")
    try:
        yield c
    finally:
        c.close()


_STRUCT = (
    "flight_id Nullable(UInt64), anchor_rank UInt8, source String, source_rank UInt8, "
    "origin_icao Nullable(String), dest_icao Nullable(String), origin_gated Nullable(UInt8), "
    "dest_gated Nullable(UInt8), icao24 String, win_start Nullable(DateTime64(6)), overlap_s Nullable(Int64)"
)
T1, T2, T3 = "2026-08-01 00:00:00", "2026-08-01 03:00:00", "2026-08-01 06:00:00"

# cand rows: (flight_id, anchor_rank, source, source_rank, origin, dest, origin_gated, dest_gated, icao24,
# win_start, overlap_s). Stage 1 groups by (source, icao24, win_start); stage 2 by (flight_id, source).
_CAND = [
    # stage 1: one opinion window seen by several spine anchors
    (101, 1, "adsblol", 4, "RJTT", "RJCC", 0, 0, "aaa", T1, 100),  # overlap beats anchor_rank -> 102
    (102, 3, "adsblol", 4, "RJTT", "RJCC", 0, 0, "aaa", T1, 500),
    (201, 2, "adsblol", 4, "RJTT", "RJCC", 0, 0, "bbb", T1, 300),  # equal overlap -> lower anchor_rank -> 202
    (202, 1, "adsblol", 4, "RJTT", "RJCC", 0, 0, "bbb", T1, 300),
    (302, 1, "adsblol", 4, "RJTT", "RJCC", 0, 0, "ccc", T1, 300),  # equal overlap+rank -> lower flight_id
    (301, 1, "adsblol", 4, "RJTT", "RJCC", 0, 0, "ccc", T1, 300),  # -> 301
    (401, 1, "swim", 1, "RJBB", "PANC", 0, 0, "ddd", T1, 300),  # SWIM round-trip filed both ways
    (401, 1, "swim", 1, "PANC", "RJBB", 0, 0, "ddd", T1, 300),  # in one window + a NULL-origin dup
    (401, 1, "swim", 1, None, "RJBB", 0, 0, "ddd", T1, 300),  # origin ASC, NULL last -> PANC/RJBB
    (501, 1, "swim", 1, "RJTT", None, 0, 0, "eee", T1, 300),  # dest NULL last -> RJAA
    (501, 1, "swim", 1, "RJTT", "RJAA", 0, 0, "eee", T1, 300),
    # stage 2: several windows of one source attached to the same flight
    (601, 1, "opensky_flights", 3, None, None, 0, 0, "fff", T1, 900),  # opensky_flights: resolvedness beats
    (601, 1, "opensky_flights", 3, "RJTT", None, 0, 0, "fff", T3, 500),  # overlap -> RJTT/RJCC
    (601, 1, "opensky_flights", 3, "RJTT", "RJCC", 0, 0, "fff", T2, 100),
    (701, 2, "adsblol", 4, "RJOO", None, 0, 0, "ggg", T1, 900),  # other sources: overlap-first -> RJOO/NULL
    (701, 2, "adsblol", 4, "RJTT", "RJCC", 0, 0, "ggg", T2, 100),
    (801, 2, "adsblol", 4, "RJCC", "RJTT", 0, 0, "hhh", T2, 300),  # equal overlap -> earlier win_start
    (801, 2, "adsblol", 4, "RJTT", "RJCC", 0, 0, "hhh", T1, 300),  # -> RJTT/RJCC
    (901, 1, "opensky_flights", 3, "RJTT", "RJCC", 0, 0, "iii", T2, 100),  # vote winner; the LOSING window's
    (901, 1, "opensky_flights", 3, None, "RJCC", 1, 0, "iii", T1, 900),  # gate hit must still flag the vote
]

_EXPECTED = [
    (102, "adsblol", 4, "RJTT", "RJCC", 0, 0),
    (202, "adsblol", 4, "RJTT", "RJCC", 0, 0),
    (301, "adsblol", 4, "RJTT", "RJCC", 0, 0),
    (401, "swim", 1, "PANC", "RJBB", 0, 0),
    (501, "swim", 1, "RJTT", "RJAA", 0, 0),
    (601, "opensky_flights", 3, "RJTT", "RJCC", 0, 0),
    (701, "adsblol", 4, "RJOO", None, 0, 0),
    (801, "adsblol", 4, "RJTT", "RJCC", 0, 0),
    (901, "opensky_flights", 3, "RJTT", "RJCC", 1, 0),
]


def _lit(v) -> str:
    if v is None:
        return "NULL"
    return f"'{v}'" if isinstance(v, str) else str(v)


def test_reducers_pick_the_documented_winners(ch):
    # Executes the model's own anchor_pick/votes SQL over fixture candidates: one case per tie-break rule, and
    # the losing anchor (101) gets no vote at all — the spanning-record guarantee.
    rows = ", ".join("(" + ", ".join(_lit(v) for v in r) + ")" for r in _CAND)
    sql = f"with cand as (select * from values('{_STRUCT}', {rows})),\n{_tail()}\norder by flight_id, source"
    got = [tuple(r) for r in ch.query(sql).result_rows]
    assert got == _EXPECTED
