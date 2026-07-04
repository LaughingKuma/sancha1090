from __future__ import annotations

import gzip
import io
import json
import tarfile
from datetime import date

from include import adsblol_backfill as ab


DAY = 1_735_689_600  # 2025-01-01 00:00 UTC


def _doc(points, icao="abc123", ts=DAY):
    return {"icao": icao, "timestamp": ts, "trace": points}


def _point(dt, lat=35.0, lon=139.0, alt=10_000, gs=100.0, track=270.0,
           rate=600.0, obj=None, alt_geom=10_500, flags=0):
    return [dt, lat, lon, alt, gs, track, flags, rate, obj, "adsb_icao", alt_geom, None, None, None]


def test_resample_emits_converted_row_on_boundary():
    rows = ab.resample_trace(_doc([_point(720)]), DAY)
    assert len(rows) == 1
    row = rows[0]
    assert row["snapshot_time"] == DAY + 720
    assert row["icao24"] == "abc123"
    assert abs(row["velocity"] - 100 * 0.514444) < 1e-9
    assert abs(row["baro_altitude"] - 10_000 * 0.3048) < 1e-9
    assert abs(row["vertical_rate"] - 600 * 0.00508) < 1e-9
    assert abs(row["geo_altitude"] - 10_500 * 0.3048) < 1e-9
    assert row["true_track"] == 270.0
    assert row["on_ground"] is False
    assert row["region"] == "japan"
    assert row["source"] == "adsblol"
    assert row["time_position"] == DAY + 720
    assert row["last_contact"] == DAY + 720


def test_resample_owns_midnight_end_not_start():
    # The day's file can't satisfy a 00:00 boundary (its points start at 00:00),
    # so days own 00:12..24:00 and tile without holes or duplicates.
    rows = ab.resample_trace(_doc([_point(0), _point(86_395)]), DAY)
    times = [r["snapshot_time"] for r in rows]
    assert DAY not in times
    assert DAY + 86_400 in times


def test_resample_stale_flag_blocks_position_use():
    # flags&1 = repeated last-known fix; usable for identity, not position.
    rows = ab.resample_trace(_doc([_point(680, lat=35.0), _point(700, lat=36.0, flags=1)]), DAY)
    assert len(rows) == 1
    assert rows[0]["latitude"] == 35.0


def test_resample_geometric_altitude_flag():
    # flags&8 = the altitude field is geometric; never write it as barometric.
    rows = ab.resample_trace(_doc([_point(720, alt=10_000, alt_geom=None, flags=8)]), DAY)
    assert rows[0]["baro_altitude"] is None
    assert abs(rows[0]["geo_altitude"] - 10_000 * 0.3048) < 1e-9


def test_resample_rejects_stale_points():
    # Last fix is 710s before the second boundary — beyond the 60s staleness window.
    rows = ab.resample_trace(_doc([_point(10)]), DAY)
    assert [r["snapshot_time"] for r in rows] == []


def test_resample_takes_latest_fresh_point_per_boundary():
    rows = ab.resample_trace(_doc([_point(600), _point(700, lat=36.0)]), DAY)
    assert len(rows) == 1
    assert rows[0]["snapshot_time"] == DAY + 720
    assert rows[0]["latitude"] == 36.0


def test_resample_ground_points():
    rows = ab.resample_trace(_doc([_point(720, alt="ground")]), DAY)
    assert rows[0]["on_ground"] is True
    assert rows[0]["baro_altitude"] is None


def test_resample_filters_outside_japan_box():
    rows = ab.resample_trace(_doc([_point(720, lat=51.5, lon=-0.1)]), DAY)
    assert rows == []


def test_resample_forward_fills_callsign_and_squawk():
    points = [
        _point(0, obj={"flight": "ANA123  ", "squawk": "2000"}),
        _point(700),
    ]
    rows = ab.resample_trace(_doc(points), DAY)
    assert all(r["callsign"] == "ANA123" for r in rows)
    assert all(r["squawk"] == "2000" for r in rows)


def test_resample_skips_tisb_synthetics():
    assert ab.resample_trace(_doc([_point(720)], icao="~26eea3"), DAY) == []


def test_iter_trace_members_yields_none_for_corrupt_gzip():
    good = gzip.compress(json.dumps(_doc([_point(720)])).encode())
    bad = b"\x1f\x8b" + b"\x99" * 64
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, payload in [
            ("./traces/23/trace_full_abc123.json", good),
            ("./traces/24/trace_full_def456.json", bad),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    members = list(ab.iter_trace_members(io.BytesIO(buf.getvalue())))
    assert [(n.split("/")[-1], d is None) for n, d in members] == [
        ("trace_full_abc123.json", False),
        ("trace_full_def456.json", True),
    ]


def test_chained_reader_read_zero_is_noop():
    reader = ab.ChainedReader([lambda: io.BytesIO(b"abc")])
    assert reader.read(0) == b""
    assert reader.read(-1) == b"abc"


def test_resample_respects_trace_base_timestamp():
    # dt offsets are relative to the trace's own timestamp, not the day start.
    rows = ab.resample_trace(_doc([_point(20)], ts=DAY + 700), DAY)
    assert rows[0]["snapshot_time"] == DAY + 720
    assert rows[0]["last_contact"] == DAY + 720


def test_dense_rows_full_resolution_in_kanto():
    rows = ab.dense_rows(_doc([_point(0), _point(3), _point(7)]), DAY)
    assert len(rows) == 3
    assert all(r["region"] == "kanto" for r in rows)
    assert [r["snapshot_time"] for r in rows] == [DAY, DAY + 3, DAY + 7]


def test_dense_rows_zone_labels_and_full_japan_catch_all():
    rows = ab.dense_rows(_doc([
        _point(0, lat=34.43, lon=135.24),
        _point(5, lat=43.0, lon=141.3),
        _point(10, lat=51.5, lon=-0.1),
    ]), DAY)
    # KIX labels kansai, Sapporo falls through to japan_dense, London is outside.
    assert [r["region"] for r in rows] == ["kansai", "japan_dense"]


def test_dense_rows_respect_flags_and_day_window():
    rows = ab.dense_rows(_doc([
        _point(10, flags=1),
        _point(20),
        _point(86_400),
    ]), DAY)
    assert [r["snapshot_time"] for r in rows] == [DAY + 20]


def test_member_icao():
    assert ab.member_icao("./traces/23/trace_full_abc123.json") == "abc123"
    assert ab.member_icao("./traces/a3/trace_full_~26eea3.json") is None


def test_part_url_bare_and_split_suffixes():
    # Sub-2GB days ship as one unsplit .tar; larger days split into .tar.aa/.tar.ab.
    assert ab.part_url("globe_history_2026", "vT").endswith("/vT.tar")
    assert ab.part_url("globe_history_2026", "vT", "aa").endswith("/vT.tar.aa")


def test_release_candidates_order_and_repos():
    cands = ab.release_candidates(date(2025, 12, 31))
    assert cands[0] == ("globe_history_2025", "v2025.12.31-planes-readsb-prod-0")
    assert ("globe_history_2026", "v2025.12.31-planes-readsb-prod-0") in cands
    tags = [t for _, t in cands]
    # prod before tmp before staging, so the canonical artifact wins when present.
    assert tags.index("v2025.12.31-planes-readsb-prod-0") < tags.index("v2025.12.31-planes-readsb-prod-0tmp")
    assert tags.index("v2025.12.31-planes-readsb-prod-0tmp") < tags.index("v2025.12.31-planes-readsb-staging-0")


def test_flights_windows_walk_backwards_and_clamp():
    windows = list(ab.flights_windows(date(2026, 6, 5), date(2026, 6, 1)))
    days = [w[0] for w in windows]
    assert days == [date(2026, 6, 4), date(2026, 6, 2), date(2026, 6, 1)]
    for _, begin_ts, end_ts in windows:
        assert 0 < end_ts - begin_ts <= 2 * 86400
    # Newest window ends at midnight after the until-day (inclusive coverage).
    assert windows[0][2] - windows[0][1] == 2 * 86400
    # Oldest window is clamped to from_day.
    assert windows[-1][2] - windows[-1][1] == 86400


def test_chained_reader_tar_roundtrip():
    payload = gzip.compress(json.dumps(_doc([_point(0)])).encode())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("./traces/23/trace_full_abc123.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        skip = tarfile.TarInfo("./traces/23/notatrace.bin")
        skip.size = 0
        tar.addfile(skip, io.BytesIO(b""))
    raw = buf.getvalue()
    half = len(raw) // 2
    reader = ab.ChainedReader([
        lambda: io.BytesIO(raw[:half]),
        lambda: io.BytesIO(raw[half:]),
    ])
    members = list(ab.iter_trace_members(reader))
    assert len(members) == 1
    name, data = members[0]
    assert "trace_full_abc123" in name
    assert json.loads(data)["icao"] == "abc123"


def test_japan_bbox_matches_regions():
    from include.regions import REGIONS

    region = REGIONS[0]
    assert ab.JAPAN_BBOX == (region["lamin"], region["lomin"], region["lamax"], region["lomax"])
