from __future__ import annotations

import io
import json
import tarfile
from datetime import date

import scripts.backfill_adsblol_routes as bar


def _tar_bytes(members: dict[str, dict]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, doc in members.items():
            data = json.dumps(doc).encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def _doc(icao):
    pt1 = [0.0, 10.0, 100.0, 2000, 200, 90, 0, 0, None, "adsb_icao", 2000, 0, 0, 0]
    pt2 = [60.0, 10.1, 100.1, 3000, 250, 90, 0, 0, None, "adsb_icao", 3000, 0, 0, 0]
    return {"icao": icao, "timestamp": 1782345600, "trace": [pt1, pt2]}


def test_day_frames_extracts_only_target_hexes():
    tar = _tar_bytes({
        "2026/06/25/traces/53/trace_full_a61c53.json": _doc("a61c53"),
        "2026/06/25/traces/99/trace_full_ffff99.json": _doc("ffff99"),
    })
    seg_df, path_df = bar._day_frames(date(2026, 6, 25), tar, {"a61c53"}, min_traces=1)
    assert seg_df.get_column("icao24").unique().to_list() == ["a61c53"]
    assert path_df.get_column("icao24").unique().to_list() == ["a61c53"]
    assert path_df.height >= seg_df.height  # every segment carries >= its endpoint fixes


def test_run_skips_days_already_in_manifest(monkeypatch):
    monkeypatch.setattr(bar, "analytics_engine", lambda: None)
    monkeypatch.setattr(bar, "_manifest_status",
                        lambda _uri, engine=None: "ch_loaded")  # noqa: ARG005 (engine kw-bound)
    monkeypatch.setattr(bar, "_target_hexes", lambda: {"a61c53"})
    opened = []
    monkeypatch.setattr(bar, "_open_release", lambda day: opened.append(day) or None)
    rc = bar.run(date(2026, 6, 25), date(2026, 6, 25), min_traces=1, dry_run=False)
    assert rc == 0 and opened == []


def test_run_continues_to_next_day_after_a_write_failure(monkeypatch):
    monkeypatch.setattr(bar, "get_bucket", lambda: "bucket")
    monkeypatch.setattr(bar, "analytics_engine", lambda: None)
    monkeypatch.setattr(bar, "_manifest_status",
                        lambda _uri, engine=None: "missing")  # noqa: ARG005 (engine kw-bound)
    monkeypatch.setattr(bar, "_target_hexes", lambda: {"a61c53"})
    recorded = []
    monkeypatch.setattr(bar.manifest, "record_load",
                        lambda uri, *_a, **_kw: recorded.append(uri))

    opened = []

    def fake_open_release(day):
        opened.append(day)
        return _tar_bytes({"2026/06/25/traces/53/trace_full_a61c53.json": _doc("a61c53")})

    monkeypatch.setattr(bar, "_open_release", fake_open_release)

    calls = {"n": 0}

    def fake_write_parquet(_df, key):
        calls["n"] += 1
        if calls["n"] == 1:  # first write of the wave — simulate a transient failure on day 1
            raise RuntimeError("write boom")
        return f"s3://bucket/{key}"

    monkeypatch.setattr(bar, "write_parquet", fake_write_parquet)

    rc = bar.run(date(2026, 6, 25), date(2026, 6, 26), min_traces=1, dry_run=False)

    assert rc == 1
    assert opened == [date(2026, 6, 25), date(2026, 6, 26)]  # day 2 still attempted
    # The failed day must leave NO manifest record (its segments URI is the resume marker).
    assert all("2026-06-26" in uri for uri in recorded) and recorded
