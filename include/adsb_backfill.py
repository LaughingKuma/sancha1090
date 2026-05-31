from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from include.adsb_iceberg import (
    ADSB_SCHEMA,
    _DOUBLE_FIELDS,
    _INT_FIELDS,
    _JSON_FIELDS,
    _LIST_FIELDS,
    _STRING_FIELDS,
)


SCHEMA_VERSION = 1
_COLUMNS = [f.name for f in ADSB_SCHEMA.fields]


def _build_pa_schema() -> pa.Schema:
    # Mirrors capture_v2._build_schema() exactly so backfill Parquet is byte-structurally
    # identical to live producer output; pinned by test_backfill_parquet_schema_matches_producer.
    fields = [("capture_ts", pa.float64())]
    fields += [(n, pa.string()) for n in _STRING_FIELDS]
    fields += [(n, pa.float64()) for n in _DOUBLE_FIELDS]
    fields += [(n, pa.int64()) for n in _INT_FIELDS]
    fields += [(n, pa.list_(pa.string())) for n in _LIST_FIELDS]
    fields += [(n, pa.string()) for n in _JSON_FIELDS]
    fields += [("_raw_json", pa.string()), ("_schema_version", pa.int32())]
    return pa.schema(fields)


PA_SCHEMA = _build_pa_schema()

_STRING_SET = set(_STRING_FIELDS)
_DOUBLE_SET = set(_DOUBLE_FIELDS)
_INT_SET = set(_INT_FIELDS)
_LIST_SET = set(_LIST_FIELDS)
_JSON_SET = set(_JSON_FIELDS)


def coerce(name: str, v):
    """Verbatim port of capture_v2._coerce so backfilled rows are byte-comparable to live bronze.
    Anything ambiguous degrades to None (raw truth survives in _raw_json), never crashes."""
    if v is None:
        return None
    if name in _STRING_SET:
        return v if isinstance(v, str) else str(v)        # alt_baro int 35000 -> "35000"; "ground" stays
    if name in _DOUBLE_SET:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if name in _INT_SET:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v) if v.is_integer() else None
        return None
    if name in _LIST_SET:
        return [str(x) for x in v] if isinstance(v, list) else None
    if name in _JSON_SET:
        return json.dumps(v, separators=(",", ":"))
    return None


def record_to_row(record: dict) -> dict:
    """A legacy {"capture_ts", "msg"} record → one bronze row. _raw_json holds the verbatim msg
    (re-serialized) so untyped readsb fields survive losslessly, exactly as live capture does."""
    msg = record.get("msg") or {}
    row = {}
    for name in _COLUMNS:
        if name == "capture_ts":
            row[name] = record["capture_ts"]
        elif name == "_raw_json":
            row[name] = json.dumps(msg, separators=(",", ":"))
        elif name == "_schema_version":
            row[name] = SCHEMA_VERSION
        else:
            row[name] = coerce(name, msg.get(name))
    return row


def utc_hour_str(capture_ts: float) -> str:
    return datetime.fromtimestamp(capture_ts, timezone.utc).strftime("%Y-%m-%dT%H")


def group_records_by_hour(records: Iterable[dict]) -> Iterator[tuple[str, list[dict]]]:
    """Yield (hour, records) groups, flushing when the UTC hour advances. Memory-bounded for the
    multi-day stream: the legacy capture wrote in time order, so one hour is buffered at a time."""
    current_hour: str | None = None
    buf: list[dict] = []
    for rec in records:
        hour = utc_hour_str(rec["capture_ts"])
        if current_hour is None:
            current_hour = hour
        if hour != current_hour:
            yield current_hour, buf
            current_hour, buf = hour, []
        buf.append(rec)
    if buf:
        yield current_hour, buf


def write_hour_parquet(rows: list[dict], path: str, filesystem=None) -> None:
    table = pa.Table.from_pylist(rows, schema=PA_SCHEMA)
    pq.write_table(table, path, compression="zstd", filesystem=filesystem)


def iter_records(lines: Iterable) -> Iterator[dict]:
    """Parse legacy JSONL lines, skipping blanks, malformed JSON, and anything that isn't a
    record (raw truth is preserved anyway — a few unparseable lines must not abort the backfill)."""
    for line in lines:
        line = line.strip() if isinstance(line, str) else line.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(rec, dict) and "capture_ts" in rec:
            yield rec


def backfill_records(records, end_before_hour: str, write_hour_fn) -> list[dict]:
    """Per UTC-hour group strictly before end_before_hour (the earliest live bronze hour — at/after
    is already in the lake), build rows and hand them to write_hour_fn(hour, rows) for persistence
    + registration. ISO hour strings compare lexicographically. Returns per-hour summaries."""
    summaries = []
    for hour, recs in group_records_by_hour(records):
        if hour >= end_before_hour:
            continue
        rows = [record_to_row(r) for r in recs]
        info = write_hour_fn(hour, rows) or {}
        summaries.append({"hour": hour, "rows": len(rows), **info})
    return summaries
