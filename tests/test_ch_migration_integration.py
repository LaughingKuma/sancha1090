from __future__ import annotations

import os

import pytest

# Executable coverage of the P8a migration MECHANICS against a live ClickHouse (skips when unreachable, so the
# pure-unit suite still runs without CH; the CI `clickhouse-migration` job runs it for real against a bare CH).
# The static DDL parse lives in test_bronze_dedup; this exercises the actual INSERT…SELECT → OPTIMIZE FINAL
# dedup, the atomic EXCHANGE + rollback, and that a name-bound MV keeps firing after the swap.

_P = "p8a_it_"
_DROP_ORDER = [f"bronze.{_P}{n}" for n in ("mv", "mvtarget", "old", "new", "live", "live_new")]


def _drop_all(c):
    for t in _DROP_ORDER:  # MV before its source/target
        c.command(f"DROP TABLE IF EXISTS {t}")


@pytest.fixture()
def ch():
    # Connect via clickhouse_connect directly (NOT include.clickhouse, which pulls in psycopg2) so this runs on a
    # lean CI runner with only clickhouse-connect installed. Connect without a default db, then create bronze — a
    # bare clickhouse-server has only `default`; on the live stack the CREATE is a no-op.
    try:
        import clickhouse_connect
        c = clickhouse_connect.get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "default"),
            password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        )
        c.command("CREATE DATABASE IF NOT EXISTS bronze")
    except Exception as e:  # no CH in this environment -> not an integration run
        pytest.skip(f"ClickHouse not reachable: {e!r}")
    _drop_all(c)
    try:
        yield c
    finally:
        _drop_all(c)
        c.close()


def _scalar(c, sql):
    return c.query(sql).result_rows[0][0]


def test_insert_select_optimize_final_collapses_replays_keeps_legit_recaptures(ch):
    # old (MergeTree) carries: a replay twin (same content, only committed_at differs) + a legit same-grain
    # recapture (same icao24/time, different lat) + two plainly-distinct rows. The fp excludes committed_at.
    ch.command(f"CREATE TABLE bronze.{_P}old "
               "(icao24 String, snapshot_time DateTime, lat Float64, committed_at DateTime) "
               "ENGINE=MergeTree ORDER BY (snapshot_time, icao24)")
    ch.command(
        f"INSERT INTO bronze.{_P}old VALUES "
        "('AAA','2026-06-01 00:00:00',1.0,'2026-06-01 00:00:05'),"   # original
        "('AAA','2026-06-01 00:00:00',1.0,'2026-06-01 00:00:09'),"   # REPLAY (only committed_at differs) -> collapses
        "('AAA','2026-06-01 00:00:00',2.0,'2026-06-01 00:00:05'),"   # legit recapture (same grain, diff lat) -> kept
        "('BBB','2026-06-01 00:00:00',1.0,'2026-06-01 00:00:05'),"   # distinct icao -> kept
        "('AAA','2026-06-01 00:05:00',1.0,'2026-06-01 00:05:05')")   # distinct time -> kept
    ch.command(f"CREATE TABLE bronze.{_P}new "
               "(icao24 String, snapshot_time DateTime, lat Float64, committed_at DateTime, "
               " _dedup_fp UInt64 MATERIALIZED cityHash64(toString(tuple(icao24, snapshot_time, lat)))) "
               "ENGINE=ReplacingMergeTree() ORDER BY (snapshot_time, icao24, _dedup_fp)")
    # SELECT * excludes the MATERIALIZED fp (CH default); new computes it on insert -> positional copy works.
    ch.command(f"INSERT INTO bronze.{_P}new SELECT * FROM bronze.{_P}old")
    ch.command(f"OPTIMIZE TABLE bronze.{_P}new FINAL")

    rows = _scalar(ch, f"SELECT count() FROM bronze.{_P}new")
    fp = _scalar(ch, f"SELECT uniqExact(_dedup_fp) FROM bronze.{_P}new")
    old_fp = _scalar(ch, f"SELECT uniqExact(cityHash64(toString(tuple(icao24, snapshot_time, lat)))) FROM bronze.{_P}old")
    assert rows == fp == old_fp == 4, "replay collapsed; the 4 content-distinct rows (incl. the recapture) survive"
    # the (AAA, t1) grain keeps BOTH the lat=1.0 and lat=2.0 rows — grain-dedup would have destroyed one.
    grain = _scalar(ch, f"SELECT count() FROM bronze.{_P}new WHERE icao24='AAA' AND snapshot_time='2026-06-01 00:00:00'")
    assert grain == 2


def test_name_bound_mv_keeps_firing_after_exchange(ch):
    ch.command(f"CREATE TABLE bronze.{_P}live (icao24 String, snapshot_time DateTime) "
               "ENGINE=MergeTree ORDER BY (snapshot_time, icao24)")
    ch.command(f"CREATE TABLE bronze.{_P}live_new "
               "(icao24 String, snapshot_time DateTime, "
               " _dedup_fp UInt64 MATERIALIZED cityHash64(toString(tuple(icao24, snapshot_time)))) "
               "ENGINE=ReplacingMergeTree() ORDER BY (snapshot_time, icao24, _dedup_fp)")
    ch.command(f"CREATE TABLE bronze.{_P}mvtarget (icao24 String, c UInt64) ENGINE=SummingMergeTree ORDER BY icao24")
    # MV is bound to the NAME bronze.<live>, so an EXCHANGE of that name must not detach it.
    ch.command(f"CREATE MATERIALIZED VIEW bronze.{_P}mv TO bronze.{_P}mvtarget AS "
               f"SELECT icao24, count() AS c FROM bronze.{_P}live GROUP BY icao24")

    ch.command(f"INSERT INTO bronze.{_P}live VALUES ('AAA','2026-06-01 00:00:00')")   # pre-swap insert -> MV fires
    ch.command(f"INSERT INTO bronze.{_P}live_new SELECT * FROM bronze.{_P}live")        # seed _new (MV NOT bound here)
    ch.command(f"EXCHANGE TABLES bronze.{_P}live AND bronze.{_P}live_new")              # atomic swap; live is now RMT
    assert _scalar(ch, f"SELECT engine FROM system.tables WHERE database='bronze' AND name='{_P}live'") == "ReplacingMergeTree"
    ch.command(f"INSERT INTO bronze.{_P}live VALUES ('AAA','2026-06-01 00:01:00')")    # post-swap insert -> MV must still fire

    assert _scalar(ch, f"SELECT sum(c) FROM bronze.{_P}mvtarget WHERE icao24='AAA'") == 2, \
        "the name-bound MV fired on both the pre- and post-EXCHANGE inserts (no reseed needed)"


def test_rollback_while_paused_preserves_data(ch):
    # The runbook leaves tableize_states PAUSED until the operator commits, so a rollback (a plain EXCHANGE-back)
    # happens with NO post-swap rows in flight. This proves that round-trip is data-preserving: the original
    # rows survive both the swap-in and the rollback. (A rollback AFTER ingestion resumed would drop the delta —
    # the script forbids it; that's why ingestion stays paused through the decision.)
    ch.command(f"CREATE TABLE bronze.{_P}live (icao24 String, snapshot_time DateTime) "
               "ENGINE=MergeTree ORDER BY (snapshot_time, icao24)")
    ch.command(f"CREATE TABLE bronze.{_P}live_new "
               "(icao24 String, snapshot_time DateTime, "
               " _dedup_fp UInt64 MATERIALIZED cityHash64(toString(tuple(icao24, snapshot_time)))) "
               "ENGINE=ReplacingMergeTree() ORDER BY (snapshot_time, icao24, _dedup_fp)")
    ch.command(f"INSERT INTO bronze.{_P}live VALUES "
               "('AAA','2026-06-01 00:00:00'),('BBB','2026-06-01 00:00:00'),('CCC','2026-06-01 00:01:00')")
    ch.command(f"INSERT INTO bronze.{_P}live_new SELECT * FROM bronze.{_P}live")
    ch.command(f"EXCHANGE TABLES bronze.{_P}live AND bronze.{_P}live_new")              # commit (live = RMT)
    assert _scalar(ch, f"SELECT engine FROM system.tables WHERE database='bronze' AND name='{_P}live'") == "ReplacingMergeTree"
    assert _scalar(ch, f"SELECT count() FROM bronze.{_P}live") == 3

    ch.command(f"EXCHANGE TABLES bronze.{_P}live AND bronze.{_P}live_new")              # rollback (while paused, no delta)
    assert _scalar(ch, f"SELECT engine FROM system.tables WHERE database='bronze' AND name='{_P}live'") == "MergeTree"
    assert _scalar(ch, f"SELECT count() FROM bronze.{_P}live") == 3, "rollback preserves the original rows, not just the engine"
    assert _scalar(ch, f"SELECT count() FROM bronze.{_P}live WHERE icao24 IN ('AAA','BBB','CCC')") == 3


def test_daily_optimize_skips_non_rmt_and_doesnt_churn_merged_partitions(ch):
    # The daily maintenance must be cheap + safe: skip a plain-MergeTree table (pre-migration — no dedup to do),
    # and never rewrite an already-single-part partition (an unrestricted OPTIMIZE FINAL churns the whole table).
    from include.clickhouse import optimize_states_final

    ch.command(f"CREATE TABLE bronze.{_P}old (icao24 String, snapshot_time DateTime) "
               "ENGINE=MergeTree ORDER BY (snapshot_time, icao24)")
    ch.command(f"INSERT INTO bronze.{_P}old VALUES ('AAA','2026-06-01 00:00:00')")
    assert optimize_states_final(table=f"{_P}old")["skipped"] is True, "must skip until the engine is ReplacingMergeTree"

    ch.command(f"CREATE TABLE bronze.{_P}new "
               "(icao24 String, snapshot_time DateTime, "
               " _dedup_fp UInt64 MATERIALIZED cityHash64(toString(tuple(icao24, snapshot_time)))) "
               "ENGINE=ReplacingMergeTree() ORDER BY (snapshot_time, icao24, _dedup_fp)")
    ch.command(f"INSERT INTO bronze.{_P}new VALUES ('AAA','2026-06-01 00:00:00')")      # one part = already merged
    assert optimize_states_final(table=f"{_P}new")["optimized"] is True
    part1 = _scalar(ch, f"SELECT name FROM system.parts WHERE database='bronze' AND table='{_P}new' AND active=1")
    optimize_states_final(table=f"{_P}new")                                              # second run
    part2 = _scalar(ch, f"SELECT name FROM system.parts WHERE database='bronze' AND table='{_P}new' AND active=1")
    assert part1 == part2, "skip_merged_partitions=1 must not rewrite an already-merged single-part partition"
