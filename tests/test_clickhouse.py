from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from conftest import seed_adsb_bundle

from include import clickhouse as ch


class _Arrow(SimpleNamespace):
    pass


def test_insert_arrow_best_effort_swallows_failures(monkeypatch):
    # The P2 invariant: a CH failure must never raise out of the dual-write.
    def boom():
        raise RuntimeError("CH down")

    monkeypatch.setattr(ch, "ch_client", boom)
    ok, rows = ch.insert_arrow_best_effort("opensky_states", _Arrow(num_rows=5))
    assert ok is False
    assert rows == 0


def test_insert_arrow_best_effort_happy_path(monkeypatch):
    calls = {}

    fake = SimpleNamespace(
        insert_arrow=lambda t, a, **_kw: calls.update(table=t, rows=a.num_rows),
        close=lambda: calls.update(closed=True),
    )
    monkeypatch.setattr(ch, "ch_client", lambda: fake)
    ok, rows = ch.insert_arrow_best_effort("opensky_states", _Arrow(num_rows=7))
    assert (ok, rows) == (True, 7)
    assert calls == {"table": "opensky_states", "rows": 7, "closed": True}


def test_command_best_effort_swallows_failures(monkeypatch):
    monkeypatch.setattr(ch, "ch_client", lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    assert ch.command_best_effort("INSERT ...") is False


def test_chunks_single_when_no_size_or_oversize():
    assert list(ch._chunks([1, 2, 3], None)) == [[1, 2, 3]]
    assert list(ch._chunks([1, 2, 3], 10)) == [[1, 2, 3]]
    assert list(ch._chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]


def test_reset_aborts_before_clearing_markers_when_truncate_fails(monkeypatch):
    # A CH-down reset must NOT wipe the Postgres markers (that would make the backfill re-INSERT
    # duplicates into the still-populated tables — plain MergeTree has no dedup).
    from include import adsb_manifest as am
    from include import manifest

    monkeypatch.setattr(manifest, "ensure_table", lambda *_a, **_k: None)
    monkeypatch.setattr(am, "ensure_table", lambda *_a, **_k: None)

    class _DownClient:
        def command(self, _sql, **_kw):
            raise RuntimeError("CH down")

        def close(self):
            pass

    monkeypatch.setattr(ch, "ch_client", lambda: _DownClient())

    cleared = {"markers": False}

    def _engine_must_not_be_used():
        cleared["markers"] = True
        raise AssertionError("markers cleared despite failed TRUNCATE")

    monkeypatch.setattr(ch, "analytics_engine", _engine_must_not_be_used)

    with pytest.raises(RuntimeError):
        ch.reset_ch_bronze()
    assert cleared["markers"] is False


def _stub_adsb_reads(monkeypatch, *, fail_for=()):
    # Stub the Garage filesystem + per-file pyarrow read so the loader is testable offline.
    import pyarrow as pa

    from include import s3_helpers

    monkeypatch.setattr(s3_helpers, "garage_pyarrow_fs", lambda: object())

    def fake_read(_fs, s3_uri):
        if any(f in s3_uri for f in fail_for):
            raise RuntimeError("S3 read 400")
        return pa.table({"hex": ["a", "b", "c"]})  # 3 rows so ch_loaded (rows) != files

    monkeypatch.setattr(ch, "_read_adsb_table", fake_read)


def test_bake_adsb_flags_decodes_dbflags_and_drops_raw_json():
    # v6.3: the loader bakes the dbFlags integer into a typed db_flags column and drops the verbatim _raw_json
    # blob (eliminated from CH). absent/null/non-object/malformed -> 0, mirroring JSONExtractInt's 2-valued contract.
    import pyarrow as pa

    t = pa.table({
        "hex": ["a", "b", "c", "d", "e", "f"],
        "_raw_json": ['{"dbFlags": 1}', '{"dbFlags": 3}', '{"dbFlags": 0}',
                      '{"other": 9}', None, "not json{"],
        "_schema_version": [1, 1, 1, 1, 1, 1],
    })
    out = ch._bake_adsb_flags(t)

    assert "_raw_json" not in out.column_names
    assert out.schema.field("db_flags").type == pa.int32()
    assert out.column("db_flags").to_pylist() == [1, 3, 0, 0, 0, 0]
    # passthrough columns intact (by-name insert, so order is immaterial)
    assert out.column("hex").to_pylist() == ["a", "b", "c", "d", "e", "f"]
    assert out.column("_schema_version").to_pylist() == [1, 1, 1, 1, 1, 1]


class _FakeGarageFS:
    def __init__(self, keys):
        self._keys = keys

    def find(self, path):
        return [k for k in self._keys if k.startswith(path)]


def _patch_rebuild_deps(monkeypatch, garage_keys, registered):
    import include.s3_helpers as s3h
    from include import adsb_manifest as am
    monkeypatch.setattr(s3h, "get_s3fs", lambda: _FakeGarageFS(garage_keys))
    monkeypatch.setattr(s3h, "get_bucket", lambda: "sancha1090")
    monkeypatch.setattr(am, "all_adsb_state_uris", lambda: registered)
    monkeypatch.setattr(ch, "command_best_effort", lambda *_a, **_k: True)


def test_rebuild_adsb_refuses_unregistered_garage_objects(monkeypatch):
    known = "sancha1090/bronze/adsb_state/dt=2026-07-01/known_5f3b.parquet"
    stray = "sancha1090/bronze/adsb_state/dt=2026-07-01/stray.parquet"
    _patch_rebuild_deps(monkeypatch, [known, stray], {f"s3://{known}"})
    with pytest.raises(ch.StrayObjectError, match="stray.parquet"):
        ch.rebuild_adsb_from_garage(target_table="adsb_states_new", mark=False)


def test_rebuild_adsb_scratch_build_does_not_mark_manifest(monkeypatch):
    # P1: a scratch/migration build (mark=False) must NOT advance the ingestion manifest — the data isn't in the
    # live table until the swap, so marking would strand files on an abort; the per-tick loader replays post-swap.
    from include import adsb_manifest as am
    known = "sancha1090/bronze/adsb_state/dt=2026-07-01/known_5f3b.parquet"
    _patch_rebuild_deps(monkeypatch, [known], {f"s3://{known}"})
    marked = {"called": False}
    monkeypatch.setattr(am, "mark_ch_loaded", lambda *_a, **_k: marked.update(called=True) or 0)

    out = ch.rebuild_adsb_from_garage(target_table="adsb_states_new", mark=False)
    assert out == {"ok": True, "marked": 0}
    assert marked["called"] is False


def test_safe_identifier_accepts_valid_rejects_injection():
    assert ch._safe_identifier("bronze") == "bronze"
    for bad in ("bronze; DROP TABLE x", "a-b", "1bad", "a b", ""):
        with pytest.raises(ValueError):
            ch._safe_identifier(bad)


def test_load_adsb_skips_unreadable_file_and_loads_rest(adsb_manifest_eng, monkeypatch):
    from include import adsb_manifest as am

    seed_adsb_bundle(adsb_manifest_eng, "good.parquet")
    seed_adsb_bundle(adsb_manifest_eng, "bad.parquet")
    _stub_adsb_reads(monkeypatch, fail_for=("bad.parquet",))
    monkeypatch.setattr(ch, "insert_arrow_best_effort", lambda _t, a, **_k: (True, a.num_rows))

    out = ch.load_adsb_pending_to_ch(engine=adsb_manifest_eng)

    # ch_loaded is ROWS (3 from the one good file), files is the file count — same shape as states/flights.
    assert out == {"ch_loaded": 3, "files": 1, "ok": False}  # good loaded, bad skipped (not fatal)
    # The unreadable file stays CH-pending so a later run retries it; the good one is marked.
    assert [p["filename"] for p in am.pending_ch_adsb_uris(engine=adsb_manifest_eng)] == ["bad.parquet"]


def test_drain_transformed_skips_missing_file_and_marks_only_loaded(monkeypatch):
    # States/flights lane: a file missing from Garage is skipped (read_pending_frames returns only the present
    # ones), so the batch isn't wedged — the present file loads + is marked, the missing one stays pending,
    # and ok=False signals the lane didn't fully drain.
    import polars as pl

    from include import manifest, s3_helpers

    batch = [{"object_uri": "s3://b/ok.parquet"}, {"object_uri": "s3://b/missing.parquet"}]
    monkeypatch.setattr(manifest, "pending_ch_uris", lambda *_a, **_k: batch)
    monkeypatch.setattr(s3_helpers, "garage_pyarrow_fs", lambda: object())
    # only the present file comes back from the reader
    monkeypatch.setattr(s3_helpers, "read_pending_frames",
                        lambda _fs, _b: ([pl.DataFrame({"x": [1]})], [batch[0]]))
    marked: list[str] = []
    monkeypatch.setattr(manifest, "mark_ch_loaded",
                        lambda uris, *_a, **_k: (marked.extend(uris), len(uris))[1])
    monkeypatch.setattr(ch, "insert_arrow_best_effort", lambda _t, a, **_k: (True, a.num_rows))

    out = ch._drain_transformed(["bronze/states"], lambda df: df, "opensky_states",
                                batch_files=None, engine=object())

    assert out == {"ch_loaded": 1, "files": 1, "ok": False}
    assert marked == ["s3://b/ok.parquet"]   # the missing file is NOT marked → stays pending for retry/cleanup


def test_load_adsb_non_blocking_when_insert_fails(adsb_manifest_eng, monkeypatch):
    from include import adsb_manifest as am

    seed_adsb_bundle(adsb_manifest_eng, "x.parquet")
    _stub_adsb_reads(monkeypatch)
    monkeypatch.setattr(ch, "insert_arrow_best_effort", lambda *_a, **_k: (False, 0))

    out = ch.load_adsb_pending_to_ch(engine=adsb_manifest_eng)

    assert out == {"ch_loaded": 0, "files": 0, "ok": False}
    assert [p["filename"] for p in am.pending_ch_adsb_uris(engine=adsb_manifest_eng)] == ["x.parquet"]


def test_load_adsb_chunks_backlog(adsb_manifest_eng, monkeypatch):
    from include import adsb_manifest as am

    seed_adsb_bundle(adsb_manifest_eng, "a.parquet")
    seed_adsb_bundle(adsb_manifest_eng, "b.parquet")
    _stub_adsb_reads(monkeypatch)
    inserts = []
    monkeypatch.setattr(ch, "insert_arrow_best_effort",
                        lambda _t, a, **_k: inserts.append(a.num_rows) or (True, a.num_rows))

    out = ch.load_adsb_pending_to_ch(engine=adsb_manifest_eng, batch_files=1)

    assert out == {"ch_loaded": 6, "files": 2, "ok": True}  # 2 files × 3 rows
    assert len(inserts) == 2  # one INSERT per file, not one oversized batch
    assert am.pending_ch_adsb_uris(engine=adsb_manifest_eng) == []


def test_transform_adsblol_segments_frame_types():
    import polars as pl

    from include.adsblol_routes import segments_frame
    from include.clickhouse import transform_adsblol_segments_frame

    raw = segments_frame([{
        "icao24": "a61c53", "callsign": "GTI518",
        "seg_start": 1782365490, "seg_end": 1782392243, "num_fixes": 5,
        "first_lat": 32.5, "first_lon": 128.0, "first_alt_ft": 31000.0, "first_on_ground": False,
        "last_lat": 61.17, "last_lon": -150.33, "last_alt_ft": 2975.0, "last_on_ground": False,
        "trace_day": "2026-06-25", "source": "adsblol",
    }])
    out = transform_adsblol_segments_frame(raw)
    assert out.schema["seg_start"] == pl.Datetime("us", "UTC")
    assert out.schema["seg_end"] == pl.Datetime("us", "UTC")
    assert out.schema["trace_day"] == pl.Date
    assert out.schema["ingested_at"] == pl.Datetime("us", "UTC")
    assert out.get_column("committed_at").to_list() == out.get_column("ingested_at").to_list()
    assert out.get_column("seg_start").dt.epoch("s").to_list() == [1782365490]


def test_transform_adsblol_frames_drop_hive_dt_column():
    # read_pending_frames reads by path, so pyarrow adds the dt= partition dir as a
    # column; the transforms must strip it or the CH insert rejects the whole batch.
    import polars as pl

    from include.adsblol_routes import paths_frame, segments_frame
    from include.clickhouse import (
        transform_adsblol_paths_frame,
        transform_adsblol_segments_frame,
    )

    seg = segments_frame([{
        "icao24": "a61c53", "callsign": "GTI518",
        "seg_start": 1782365490, "seg_end": 1782392243, "num_fixes": 5,
        "first_lat": 32.5, "first_lon": 128.0, "first_alt_ft": 31000.0, "first_on_ground": False,
        "last_lat": 61.17, "last_lon": -150.33, "last_alt_ft": 2975.0, "last_on_ground": False,
        "trace_day": "2026-06-25", "source": "adsblol",
    }]).with_columns(pl.lit("2026-06-25").alias("dt"))
    assert "dt" not in transform_adsblol_segments_frame(seg).columns

    pth = paths_frame([{
        "icao24": "a61c53", "seg_start": 1782365490, "ts": 1782365500,
        "lat": 32.5, "lon": 128.0, "alt_ft": 31000.0, "on_ground": False,
        "gs_kt": 480.0, "track_deg": 55.0,
        "trace_day": "2026-06-25", "source": "adsblol",
    }]).with_columns(pl.lit("2026-06-25").alias("dt"))
    assert "dt" not in transform_adsblol_paths_frame(pth).columns


def test_transform_adsblol_paths_frame_types():
    import polars as pl

    from include.adsblol_routes import paths_frame
    from include.clickhouse import transform_adsblol_paths_frame

    raw = paths_frame([{
        "icao24": "a61c53", "seg_start": 1782365490, "ts": 1782365500,
        "lat": 32.5, "lon": 128.0, "alt_ft": 31000.0, "on_ground": False,
        "gs_kt": 480.0, "track_deg": 55.0,
        "trace_day": "2026-06-25", "source": "adsblol",
    }])
    out = transform_adsblol_paths_frame(raw)
    assert out.schema["seg_start"] == pl.Datetime("us", "UTC")
    assert out.schema["ts"] == pl.Datetime("us", "UTC")
    assert out.schema["trace_day"] == pl.Date
    assert out.get_column("ts").dt.epoch("s").to_list() == [1782365500]


class _OptimizeClient:
    # Records every command so a test can pin the exact OPTIMIZE text the DAG will send.
    def __init__(self, parts):
        self.parts = parts
        self.commands = []
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        rows = self.parts if "system.parts" in sql else [("ReplacingMergeTree",)]
        return SimpleNamespace(result_rows=rows)

    def command(self, sql, **_kw):
        self.commands.append(sql)

    def close(self):
        pass


_SEP = datetime(2026, 9, 8, 18, 30, tzinfo=timezone.utc)
_OCT = datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)
_PARTITION_FINAL = (
    "OPTIMIZE TABLE {db}.swim_flightdata PARTITION ID '{pid}' FINAL SETTINGS optimize_skip_merged_partitions = 1"
)


def _optimize(monkeypatch, table, parts, now=_SEP, live_month_final=False):
    fake = _OptimizeClient(parts)
    monkeypatch.setattr(ch, "ch_client", lambda **_kw: fake)
    return ch.optimize_states_final(table, live_month_final=live_month_final, now=now), fake


def test_swim_optimize_mid_month_touches_nothing(monkeypatch):
    # #197: the live month is multi-part every night (a part per 5-min tableize), so it must never be FINAL'd.
    result, fake = _optimize(monkeypatch, "swim_flightdata", [("202607", 1), ("202608", 1), ("202609", 7)])
    assert fake.commands == []
    assert result == {"optimized": False, "skipped": False, "partitions": []}


@pytest.mark.parametrize("parts, now", [
    ([("202608", 1), ("202609", 41), ("202610", 3)], _OCT),
    # 00:05 Oct 1 with no October part yet: the closed month is the max partition, and it must still get its FINAL.
    ([("202608", 1), ("202609", 41)], datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)),
])
def test_swim_optimize_first_run_of_new_month_finals_previous_month_only(monkeypatch, parts, now):
    result, fake = _optimize(monkeypatch, "swim_flightdata", parts, now=now)
    assert fake.commands == [_PARTITION_FINAL.format(db=ch._CH_DB, pid="202609")]
    assert result["partitions"] == ["202609"] and result["optimized"] is True


def test_swim_optimize_previous_month_already_single_part_is_idempotent(monkeypatch):
    # The rollover FINAL keys on system.parts, not a stored flag: once single-part it is never rewritten again.
    _, fake = _optimize(monkeypatch, "swim_flightdata", [("202609", 1), ("202610", 9)], now=_OCT)
    assert fake.commands == []


def test_swim_optimize_future_dated_partition_never_makes_the_live_month_closed(monkeypatch, caplog):
    # One message with a future sourceTimeStamp lands a 202701 part; max(partition_id) would FINAL live 202609.
    with caplog.at_level("WARNING", logger="include.clickhouse"):
        result, fake = _optimize(monkeypatch, "swim_flightdata", [("202609", 7), ("202701", 2)])
    assert fake.commands == []
    assert result["partitions"] == []
    assert "202701" in caplog.text and "202609" in caplog.text


def test_closed_multipart_partitions_orders_and_skips_live_and_future():
    assert ch.closed_multipart_partitions([], now=_SEP) == []
    assert ch.closed_multipart_partitions([("202609", 5)], now=_SEP) == []
    assert ch.closed_multipart_partitions([("202609", 2), ("202607", 3), ("202608", 1)], now=_SEP) == ["202607"]
    assert ch.closed_multipart_partitions([("202607", 3), ("202701", 4), ("202609", 9)], now=_SEP) == ["202607"]
    # `now` in another zone still picks the UTC month: 2026-10-01 07:00 JST is 2026-09-30 22:00 UTC.
    jst = datetime(2026, 10, 1, 7, 0, tzinfo=timezone(timedelta(hours=9)))
    assert ch.closed_multipart_partitions([("202609", 9)], now=jst) == []


def test_live_month_final_keeps_the_unrestricted_final(monkeypatch):
    # The partition rule is the parameter, not the table name; the default is the byte-identical unrestricted FINAL.
    for table in ("opensky_states", "adsb_states"):
        result, fake = _optimize(monkeypatch, table, [("202609", 7)], live_month_final=True)
        assert fake.commands == [f"OPTIMIZE TABLE {ch._CH_DB}.{table} FINAL SETTINGS optimize_skip_merged_partitions = 1"]
        assert result == {"optimized": True, "skipped": False}
        assert not any("system.parts" in q for q in fake.queries)
