from dataclasses import asdict
from datetime import datetime, timezone
import collections
import hashlib
import importlib.util
import json
import os
import threading
import uuid


def _load_sibling(name):
    # File-relative loading works both in the baked image and under spec-loaded tests.
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


est = _load_sibling("estimator")

METHOD_VERSION = "gc-dr-1"
UNCERTAINTY_BANDS: dict = {
    "gap_2_10m": {"p50_km": 0.2, "p90_km": 4.2},
    "gap_15_60m": {"p50_km": 5.5, "p90_km": 27.9},
    "gap_60_180m": {"p50_km": 44.4, "p90_km": 181.4},
    # n=1 is not a band, so the 60–180m values are served explicitly as a floor.
    "gap_180m_plus": {"p50_km": 44.4, "p90_km": 181.4, "floor": True},
    # Route-prior bins carry base band VALUES but serve them as FLOORS: the bands were
    # calibrated on GC bridges, and an admitted prior can sit far off that GC (adversarial r6).
    "gap_2_10m_route": {"p50_km": 0.2, "p90_km": 4.2, "floor": True},
    "gap_15_60m_route": {"p50_km": 5.5, "p90_km": 27.9, "floor": True},
    "gap_60_180m_route": {"p50_km": 44.4, "p90_km": 181.4, "floor": True},
    "gap_180m_plus_route": {"p50_km": 44.4, "p90_km": 181.4, "floor": True},
    "dest_ext": {"p50_km": 16.4, "p90_km": 47.2},
    "origin_ext": {"p50_km": 13.2, "p90_km": 35.9},
    "dr": {"p50_km": 1.3, "p90_km": 20.2},
}

# one catalogue: column and CH type stay adjacent, so an insert-order edit can't desync the two lists
_SCHEMA: list[tuple[str, str]] = [
    ("estimate_id", "UUID"),
    ("producer", "LowCardinality(String)"),
    ("flight_id", "Nullable(UInt64)"),
    ("icao24", "LowCardinality(String)"),
    ("computed_at", "DateTime64(3, 'UTC')"),
    ("method_version", "LowCardinality(String)"),
    ("config_hash", "UInt64"),
    ("wind_source", "LowCardinality(String)"),
    ("wind_mode", "LowCardinality(String)"),
    ("wind_model", "LowCardinality(String)"),
    ("wind_run_at", "Nullable(DateTime)"),
    ("wind_generation", "String"),
    ("wind_samples", (
        "Array(Tuple(seg_idx UInt8, lat Float64, lon Float64, alt_ft Float64, ts UInt32, "
        "u_kt Float64, v_kt Float64, gen String))"
    )),
    ("input_provisional", "UInt8"),
    ("input_as_of", "DateTime"),
    ("anchor_ts", "Nullable(DateTime)"),
    ("input_first_ts", "Nullable(DateTime)"),
    ("input_last_ts", "Nullable(DateTime)"),
    ("input_fingerprint", "UInt64"),
    ("seg_idx", "UInt8"),
    ("kind", "LowCardinality(String)"),
    ("points", "Array(Tuple(ts UInt32, lat Float64, lon Float64, alt_ft Nullable(Float64)))"),
    ("gs_entry_kt", "Nullable(Float32)"),
    ("gs_exit_kt", "Nullable(Float32)"),
    ("tas_carried_kt", "Nullable(Float32)"),
    ("capped", "UInt8"),
    ("uncertainty_bin", "LowCardinality(String)"),
    ("uncertainty_p50_km", "Nullable(Float32)"),
    ("uncertainty_p90_km", "Nullable(Float32)"),
    ("skips", "Array(Tuple(kind LowCardinality(String), reason LowCardinality(String)))"),
    ("meta_json", "String"),
]

INSERT_COLUMNS: list[str] = [c for c, _ in _SCHEMA]
INSERT_TYPES: list[str] = [t for _, t in _SCHEMA]


class LogQueue:
    # locked throughout: the shutdown tail drain can overlap a cancelled-but-still-running writer
    # thread, and an unsynchronized drain can vanish a popped group without inserting or counting it
    def __init__(self, max_groups):
        self._max_groups = max(0, int(max_groups))
        self._queued = collections.deque()
        self._lock = threading.Lock()
        self.dropped = 0
        self.accepted = 0
        self.written = 0

    @property
    def groups(self):
        return len(self._queued)

    def put(self, rows):
        with self._lock:
            if len(self._queued) >= self._max_groups:
                self.dropped += 1
                return
            self.accepted += 1
            self._queued.append(list(rows))

    def record_written(self, ngroups):
        with self._lock:
            self.written += ngroups

    def record_drop(self, ngroups):
        with self._lock:
            self.dropped += ngroups

    def drain(self, max_rows):
        with self._lock:
            if not self._queued:
                return [], 0
            rows = list(self._queued.popleft())
            ngroups = 1
            while self._queued and len(rows) + len(self._queued[0]) <= max_rows:
                rows.extend(self._queued.popleft())
                ngroups += 1
            return rows, ngroups


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, default=str)


def _json_uint64(value):
    canonical = _canonical_json(value).encode("utf-8")
    return int.from_bytes(hashlib.sha256(canonical).digest()[:8], "big")


CONFIG_HASH: int = _json_uint64(asdict(est.DEFAULT_CONFIG))


def input_fingerprint(points, od, route_pts=None, route_plan_ts=None) -> int:
    od_tuple = (
        od.origin.lat,
        od.origin.lon,
        od.origin.source,
        od.origin.agreement,
        od.dest.lat,
        od.dest.lon,
        od.dest.source,
        od.dest.agreement,
    )
    canonical_input = {
        "points": list(points),
        "od": od_tuple,
        # null != any token list, and plan_ts rides the hash: a plan appearing OR a same-token
        # amendment between two serves must miss the estimate cache (r1 finding)
        "route": None if route_pts is None
        else {"pts": [list(p) for p in route_pts], "plan_ts": route_plan_ts},
        "config": asdict(est.DEFAULT_CONFIG),
    }
    return _json_uint64(canonical_input)


def new_estimate_id() -> uuid.UUID:
    return uuid.uuid4()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _segment_meta(segment):
    bin_name = segment.meta["bin"]
    band = UNCERTAINTY_BANDS[bin_name]
    served = {
        "gs_entry_kt": segment.meta["gs_entry_kt"],
        "gs_exit_kt": segment.meta["gs_exit_kt"],
        "tas_carried_kt": None,
        "capped": segment.meta["capped"],
        "wind": {"source": "none", "coverage": 0.0},
        "uncertainty": {
            "p50_km": band["p50_km"],
            "p90_km": band["p90_km"],
            "bin": bin_name,
            "floor": bool(band.get("floor", False)),
        },
        "confidence": segment.meta["confidence"],
    }
    # route provenance rides the served meta so meta_json stays a lossless record of the wire
    if segment.meta.get("route") is not None:
        served["route"] = segment.meta["route"]
    return served


def _segments_payload(result):
    return [
        {"kind": segment.kind, "points": segment.points, "meta": _segment_meta(segment)}
        for segment in result.segments
    ]


def build_response(flight_id: str, result, provisional: bool, as_of: int) -> dict:
    return {
        "flight_id": flight_id,
        "segments": _segments_payload(result),
        "skips": result.skips,
        "method_version": METHOD_VERSION,
        "wind_source": "none",
        "input_provisional": provisional,
        "input_as_of": as_of,
    }


def build_live_response(icao24: str, result, as_of: int) -> dict:
    # live wire/log carry only dr-arm skips: OD() is synthesized for live, so origin/dest
    # eligibility noise is meaningless there; 'all' rides the uniform empty shape (design §5)
    return {
        "flight_id": None,
        "icao24": icao24,
        "segments": _segments_payload(result),
        "skips": [s for s in result.skips if s["kind"] in ("dr", "all")],
        "method_version": METHOD_VERSION,
        "wind_source": "none",
        "input_provisional": False,
        "input_as_of": as_of,
    }


def _as_utc_datetime(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _input_bounds(points):
    if not points:
        return None, None
    timestamps = [point[0] for point in points]
    return _as_utc_datetime(min(timestamps)), _as_utc_datetime(max(timestamps))


def _ordered_row(values):
    return tuple(values[column] for column in INSERT_COLUMNS)


def build_log_rows(
    estimate_id, fid, icao24, result, payload, points, fingerprint, computed_at, anchor_ts=None,
    producer="serving",
) -> list[tuple]:
    input_first_ts, input_last_ts = _input_bounds(points)
    common = {
        "estimate_id": estimate_id,
        "producer": producer,
        "flight_id": fid,
        # rev 10.4(1): flight_id is a build-generation hash (dies at settlement);
        # the hex is the durable settlement key, so fid-keyed rows log it too
        "icao24": (icao24 or "").lower(),
        "computed_at": computed_at,
        "method_version": METHOD_VERSION,
        "config_hash": CONFIG_HASH,
        "wind_source": "none",
        "wind_mode": "",
        "wind_model": "",
        "wind_run_at": None,
        "wind_generation": "",
        "wind_samples": [],
        "input_provisional": int(bool(payload["input_provisional"])),
        "input_as_of": _as_utc_datetime(payload["input_as_of"]),
        "anchor_ts": _as_utc_datetime(anchor_ts) if anchor_ts is not None else None,
        "input_first_ts": input_first_ts,
        "input_last_ts": input_last_ts,
        "input_fingerprint": fingerprint,
    }
    skips = [(skip["kind"], skip["reason"]) for skip in payload["skips"]]
    request = {
        **common,
        "seg_idx": 0,
        "kind": "request",
        "points": [],
        "gs_entry_kt": None,
        "gs_exit_kt": None,
        "tas_carried_kt": None,
        "capped": 0,
        "uncertainty_bin": "",
        "uncertainty_p50_km": None,
        "uncertainty_p90_km": None,
        "skips": skips,
        "meta_json": _canonical_json({}),
    }
    rows = [_ordered_row(request)]
    if result is None:
        return rows

    for seg_idx, segment in enumerate(result.segments, start=1):
        served_meta = payload["segments"][seg_idx - 1]["meta"]
        uncertainty = served_meta["uncertainty"]
        segment_row = {
            **common,
            "seg_idx": seg_idx,
            "kind": segment.kind,
            "points": [
                (point[2], point[1], point[0], point[3]) for point in segment.points
            ],
            "gs_entry_kt": segment.meta["gs_entry_kt"],
            "gs_exit_kt": segment.meta["gs_exit_kt"],
            "tas_carried_kt": served_meta["tas_carried_kt"],
            "capped": int(bool(segment.meta["capped"])),
            "uncertainty_bin": uncertainty["bin"],
            "uncertainty_p50_km": uncertainty["p50_km"],
            "uncertainty_p90_km": uncertainty["p90_km"],
            "skips": [],
            "meta_json": _canonical_json(served_meta),
        }
        rows.append(_ordered_row(segment_row))
    return rows
