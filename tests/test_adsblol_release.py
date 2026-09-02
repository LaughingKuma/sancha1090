from __future__ import annotations

import gzip
import io
import json
import tarfile
from datetime import date

import pytest
import sqlalchemy as sa

import include.adsblol_route_ledger as ledger
import include.adsblol_release as rel

DAY = date(2026, 6, 25)
DAY_ISO = "2026-06-25"


@pytest.fixture
def eng():
    e = sa.create_engine("sqlite://")
    ledger.ensure_table(e)
    return e


@pytest.fixture
def caps(monkeypatch):
    written: dict[str, int] = {}
    recorded: list[tuple[str, int]] = []
    monkeypatch.setattr(rel, "write_parquet",
                        lambda df, key: written.update({key: df.height}) or f"s3://b/{key}")
    monkeypatch.setattr(rel.manifest, "record_load",
                        lambda uri, _smin, _smax, rows, engine=None:  # noqa: ARG005 (engine kw-bound)
                        recorded.append((uri, rows)))
    return {"written": written, "recorded": recorded}


def _doc(icao):
    pt1 = [0.0, 10.0, 100.0, 2000, 200, 90, 0, 0, None, "adsb_icao", 2000, 0, 0, 0]
    pt2 = [60.0, 10.1, 100.1, 3000, 250, 90, 0, 0, None, "adsb_icao", 3000, 0, 0, 0]
    return {"icao": icao, "timestamp": 1782345600, "trace": [pt1, pt2]}


def _tar_bytes(members: dict[str, bytes]) -> io.BytesIO:
    # Real releases hold gzip payloads under a bare .json name.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    buf.seek(0)
    return buf


def _member(hexid: str) -> tuple[str, bytes]:
    return (f"2026/06/25/traces/{hexid[-2:]}/trace_full_{hexid}.json",
            gzip.compress(json.dumps(_doc(hexid)).encode()))


def _tar(*hexes: str, corrupt: tuple[str, ...] = (), extra: tuple[str, ...] = ()) -> io.BytesIO:
    members = dict(_member(h) for h in hexes)
    members.update(dict(_member(h) for h in extra))
    for h in corrupt:
        members[f"2026/06/25/traces/{h[-2:]}/trace_full_{h}.json"] = b"\x1f\x8bbroken-gzip"
    return _tar_bytes(members)


# 100 clean members keep the single corrupt one inside the 1% ceiling measured on EXTRACTED members.
CLEAN = [f"a{i:05x}" for i in range(100)]


def test_land_release_day_landed_missing_error_and_writes(eng, caps):
    reader = _tar(*CLEAN, corrupt=("badc0d",), extra=("ffff99",))
    targets = [*CLEAN, "badc0d", "deadbe"]
    res = rel.land_release_day(DAY, targets, engine=eng, reader=reader, min_traces=1)

    assert res["streamed"] is True
    assert (res["repo"], res["tag"], res["parts"]) == (None, None, 0)
    assert res["members"] == 102        # keep() saw the non-target member too
    assert res["extracted"] == 101      # ...but only the targets were decompressed
    assert res["targets"] == 102 and res["fetched"] == 102
    assert (res["landed"], res["errors"], res["missing"]) == (100, 1, 1)
    assert res["landed_hexes"] == sorted(CLEAN)
    assert res["rows"] > 0 and res["path_rows"] > 0

    seg = [k for k in caps["written"] if k.startswith("bronze/adsblol_flight_segments/")]
    paths = [k for k in caps["written"] if k.startswith("bronze/adsblol_flight_paths/")]
    assert len(seg) == 1 and len(paths) == 1
    assert seg[0].startswith(f"bronze/adsblol_flight_segments/dt={DAY_ISO}/part-")
    assert paths[0].startswith(f"bronze/adsblol_flight_paths/dt={DAY_ISO}/part-")
    assert res["uri"] == f"s3://b/{seg[0]}"
    assert sorted(r for _, r in caps["recorded"]) == sorted([res["rows"], res["path_rows"]])

    with eng.begin() as conn:
        rows = dict(conn.execute(sa.text(
            "SELECT icao24, outcome FROM adsblol_route_attempts")).all())
    assert rows == {**{h: "landed" for h in CLEAN}, "badc0d": "error", "deadbe": "missing"}


def test_all_targets_landed_returns_without_streaming(eng, caps, monkeypatch):
    ledger.record_attempts([("a61c53", DAY_ISO, "landed")], eng)

    def _boom(*_a, **_kw):
        raise AssertionError("find_release must not be called when nothing is pending")

    monkeypatch.setattr(rel, "find_release", _boom)
    res = rel.land_release_day(DAY, ["a61c53"], engine=eng)
    assert res["streamed"] is False
    assert res["targets"] == 1 and res["fetched"] == 0
    assert res["members"] == res["landed"] == res["missing"] == res["errors"] == 0
    assert res["rows"] == res["path_rows"] == 0 and res["uri"] is None
    assert caps["written"] == {} and caps["recorded"] == []


def test_dry_run_counts_without_writing_or_recording(eng, caps):
    res = rel.land_release_day(DAY, ["a61c53", "deadbe"], engine=eng,
                               reader=_tar("a61c53"), min_traces=1, dry_run=True)
    assert (res["landed"], res["missing"], res["rows"] > 0) == (1, 1, True)
    assert caps["written"] == {} and caps["recorded"] == []
    with eng.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM adsblol_route_attempts")).scalar() == 0


def test_quality_gate_below_min_traces_records_nothing(eng, caps):
    with pytest.raises(RuntimeError, match="quality gate"):
        rel.land_release_day(DAY, ["a61c53"], engine=eng, reader=_tar("a61c53"), min_traces=10_000)
    assert caps["written"] == {} and caps["recorded"] == []
    with eng.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM adsblol_route_attempts")).scalar() == 0


def test_quality_gate_over_corrupt_ratio():
    # Bad members are measured against EXTRACTED members over an absolute floor of 2, so a 4-hex
    # resegment day survives one truncated member while a big day still holds the 1% ceiling.
    rel.quality_gate(members=50_000, bad=2, extracted=4, min_traces=1)
    with pytest.raises(RuntimeError, match="quality gate"):
        rel.quality_gate(members=50_000, bad=3, extracted=4, min_traces=1)
    rel.quality_gate(members=50_000, bad=10, extracted=1_000, min_traces=1)
    with pytest.raises(RuntimeError, match="quality gate"):
        rel.quality_gate(members=50_000, bad=11, extracted=1_000, min_traces=1)


def test_segmentation_failures_count_toward_the_gate(eng, caps, monkeypatch):
    def _boom(*_a, **_kw):
        raise ValueError("segmenter broke")

    monkeypatch.setattr(rel.routes, "trace_segments", _boom)
    with pytest.raises(RuntimeError, match="quality gate"):
        rel.land_release_day(DAY, CLEAN, engine=eng, reader=_tar(*CLEAN), min_traces=1)
    assert caps["written"] == {} and caps["recorded"] == []


def test_release_unavailable_when_nothing_is_published(eng, monkeypatch):
    monkeypatch.setattr(rel, "find_release", lambda _day: None)
    with pytest.raises(rel.ReleaseUnavailable, match=DAY_ISO):
        rel.land_release_day(DAY, ["a61c53"], engine=eng)


def _urlopen_raising(monkeypatch, failures: int):
    monkeypatch.setattr(rel.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _urlopen(_req, timeout=None):  # noqa: ARG001 (timeout is urllib's kwarg)
        calls["n"] += 1
        if calls["n"] <= failures:
            raise TimeoutError("timed out")
        return io.BytesIO(b"")

    monkeypatch.setattr(rel.urllib.request, "urlopen", _urlopen)
    return calls


def test_head_ok_retries_a_transient_timeout(monkeypatch):
    calls = _urlopen_raising(monkeypatch, failures=2)
    assert rel.head_ok("https://example.invalid/x.tar") is True
    assert calls["n"] == 3


def test_head_ok_raises_after_three_timeouts(monkeypatch):
    calls = _urlopen_raising(monkeypatch, failures=3)
    with pytest.raises(rel.ProbeFailed, match="kept failing"):
        rel.head_ok("https://example.invalid/x.tar")
    assert calls["n"] == 3


def _head_map(monkeypatch, present: set[str]):
    monkeypatch.setattr(rel.time, "sleep", lambda _s: None)
    monkeypatch.setattr(rel, "head_ok", lambda url: url in present)


def _url(tag: str, part: str = "") -> str:
    from include import adsblol_backfill as ab

    return ab.part_url("globe_history_2026", tag, part)


PROD = "v2026.06.25-planes-readsb-prod-0"
STAGING = "v2026.06.25-planes-readsb-staging-0"


def test_find_release_prefers_prod_over_staging(monkeypatch):
    _head_map(monkeypatch, {_url(PROD), _url(STAGING)})
    ref = rel.find_release(DAY)
    assert ref == rel.ReleaseRef("globe_history_2026", PROD, ("",))


def test_find_release_kinds_skips_prod(monkeypatch):
    _head_map(monkeypatch, {_url(PROD), _url(STAGING)})
    ref = rel.find_release(DAY, kinds=("staging-0",))
    assert ref.tag == STAGING


def test_find_release_detects_split_parts(monkeypatch):
    _head_map(monkeypatch, {_url(PROD, "aa"), _url(PROD, "ab")})
    ref = rel.find_release(DAY)
    assert ref.parts == ("aa", "ab")


def test_find_release_none_when_nothing_exists(monkeypatch):
    _head_map(monkeypatch, set())
    assert rel.find_release(DAY) is None


def test_find_release_propagates_a_head_failure(monkeypatch):
    monkeypatch.setattr(rel.time, "sleep", lambda _s: None)

    def _boom(_url):
        raise RuntimeError("HEAD kept failing")

    monkeypatch.setattr(rel, "head_ok", _boom)
    with pytest.raises(RuntimeError, match="HEAD kept failing"):
        rel.find_release(DAY)


REF = rel.ReleaseRef("globe_history_2026", PROD, ("",))


def test_land_day_if_due_skips_an_already_landed_day(eng, monkeypatch):
    ledger.record_release_landing(DAY_ISO, repo="globe_history_2026", tag=PROD, parts=1,
                                  members=9, targets=2, landed=2, missing=0, errors=0, engine=eng)

    def _boom(*_a, **_kw):
        raise AssertionError("find_release must not be called for a landed day")

    monkeypatch.setattr(rel, "find_release", _boom)
    res = rel.land_day_if_due(DAY, engine=eng)
    assert res["status"] == "already_landed"
    assert res["tag"] == PROD and res["landed"] == 2


def test_land_day_if_due_no_targets_leaves_no_marker(eng, monkeypatch):
    monkeypatch.setattr(rel.routes, "release_targets", lambda _day: [])
    monkeypatch.setattr(rel, "find_release", lambda *_a, **_kw: REF)
    assert rel.land_day_if_due(DAY, engine=eng)["status"] == "no_targets"
    assert ledger.release_landing(DAY_ISO, eng) is None


def test_land_day_if_due_unpublished_leaves_no_marker(eng, monkeypatch):
    monkeypatch.setattr(rel.routes, "release_targets", lambda _day: ["a61c53"])
    monkeypatch.setattr(rel, "find_release", lambda *_a, **_kw: None)
    assert rel.land_day_if_due(DAY, engine=eng)["status"] == "unpublished"
    assert ledger.release_landing(DAY_ISO, eng) is None


def test_land_day_if_due_records_the_marker(eng, caps, monkeypatch):  # noqa: ARG001 (caps quiets writes)
    monkeypatch.setattr(rel.routes, "release_targets", lambda _day: ["a61c53", "deadbe"])
    monkeypatch.setattr(rel, "find_release", lambda *_a, **_kw: REF)
    monkeypatch.setattr(rel, "open_release", lambda _ref: _tar("a61c53"))

    res = rel.land_day_if_due(DAY, engine=eng, min_traces=1)
    assert res["status"] == "landed" and res["repo"] == "globe_history_2026"
    marker = ledger.release_landing(DAY_ISO, eng)
    assert marker["tag"] == PROD and marker["parts"] == 1
    assert (marker["members"], marker["targets"]) == (1, 2)
    assert (marker["landed"], marker["missing"], marker["errors"]) == (1, 1, 0)


def test_land_day_if_due_force_relands(eng, caps, monkeypatch):  # noqa: ARG001 (caps quiets writes)
    monkeypatch.setattr(rel.routes, "release_targets", lambda _day: ["a61c53"])
    monkeypatch.setattr(rel, "find_release", lambda *_a, **_kw: REF)
    monkeypatch.setattr(rel, "open_release", lambda _ref: _tar("a61c53"))
    assert rel.land_day_if_due(DAY, engine=eng, min_traces=1)["status"] == "landed"
    # Without force the marker short-circuits; with force the day streams again.
    assert rel.land_day_if_due(DAY, engine=eng, min_traces=1)["status"] == "already_landed"
    # force clears the day's landed attempt rows itself, so the whole day re-extracts.
    res = rel.land_day_if_due(DAY, engine=eng, force=True, min_traces=1)
    assert res["status"] == "landed" and res["streamed"] is True
    assert (res["fetched"], res["landed"], res["rows"] > 0) == (1, 1, True)


def test_land_day_if_due_force_keeps_marker_when_unpublished(eng, caps, monkeypatch):  # noqa: ARG001
    monkeypatch.setattr(rel.routes, "release_targets", lambda _day: ["a61c53"])
    monkeypatch.setattr(rel, "find_release", lambda *_a, **_kw: REF)
    monkeypatch.setattr(rel, "open_release", lambda _ref: _tar("a61c53"))
    assert rel.land_day_if_due(DAY, engine=eng, min_traces=1)["status"] == "landed"
    before = rel.ledger.release_landing(DAY.isoformat(), eng)
    # A forced re-land whose pinned variant has no release must leave the real landing record alone.
    monkeypatch.setattr(rel, "find_release", lambda *_a, **_kw: None)
    assert rel.land_day_if_due(DAY, engine=eng, force=True, min_traces=1)["status"] == "unpublished"
    assert rel.ledger.release_landing(DAY.isoformat(), eng) == before


def test_sweep_days_is_four_days_oldest_first():
    assert rel.sweep_days(date(2026, 7, 10)) == [date(2026, 7, 7), date(2026, 7, 8),
                                                 date(2026, 7, 9), date(2026, 7, 10)]
