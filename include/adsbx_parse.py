from __future__ import annotations

import gzip
import io
import json
import tempfile

import polars as pl

# Measured against the live file (2026-08-13, 614,565 lines; docs/notes/2026-08-12-adsbx-malformed-record.md
# Round 4): every consumed field is absent-as-null or exactly one JSON type, derived not guessed.
_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "icao": (str,),
    "reg": (str,),
    "icaotype": (str,),
    "short_type": (str,),
    "year": (str,),
    "manufacturer": (str,),
    "model": (str,),
    "ownop": (str,),
    "faa_pia": (bool,),
    "faa_ladd": (bool,),
    "mil": (bool,),
}

_NDJSON_SCHEMA = {
    "icao": pl.Utf8,
    "reg": pl.Utf8,
    "icaotype": pl.Utf8,
    "short_type": pl.Utf8,
    "year": pl.Utf8,
    "manufacturer": pl.Utf8,
    "model": pl.Utf8,
    "ownop": pl.Utf8,
    "faa_pia": pl.Boolean,
    "faa_ladd": pl.Boolean,
    "mil": pl.Boolean,
}

_READ_KWARGS = {"schema": _NDJSON_SCHEMA, "ignore_errors": True}

# SchemaError joins the belt: explicit schema/projection close the combination class at scan time
# (round 4), but probe and net must still share one tuple so neither can leak past the other.
_PARSE_ERRORS = (pl.exceptions.ComputeError, pl.exceptions.SchemaError)


def _reject_constant(name: str) -> None:
    # json.loads accepts non-standard NaN/Infinity/-Infinity by default; force them to reject
    # like any other malformed line instead of reaching polars uncounted.
    raise ValueError(f"non-standard JSON constant: {name}")


def _type_violation(obj: dict) -> bool:
    # Absent field is never a violation (uniform-key projection nulls it); only a present,
    # non-null value outside the measured contract counts.
    for field, types in _FIELD_TYPES.items():
        v = obj.get(field)
        if v is not None and type(v) not in types:
            return True
    return False


def _project(obj: dict) -> bytes:
    # Re-serialize to exactly the consumed field set, uniform key order, missing -> null: strips
    # unconsumed junk so it can no longer trigger any polars schema behavior, combination or not.
    return json.dumps({field: obj.get(field) for field in _FIELD_TYPES}).encode()


def _drop_rate_check(rejected: int, total: int, max_drop_rate: float) -> None:
    drop_rate = rejected / total if total else 0.0
    if drop_rate > max_drop_rate:
        raise ValueError(f"basic-ac-db drop rate too high ({rejected}/{total} = {drop_rate:.4%}) — refusing to land")


def _bisect_bad_lines(lines: list[bytes]) -> set[int]:
    # Failure-path-only net: identify projected-but-polars-incompatible line(s) by halving.
    # Recurses into BOTH halves so two bad lines on opposite sides are both found.
    if not lines:
        # an empty accepted set (empty upstream file) must fall through to the re-raise, not recurse
        return set()
    if len(lines) == 1:
        return {0}
    mid = len(lines) // 2
    bad: set[int] = set()
    for half, offset in ((lines[:mid], 0), (lines[mid:], mid)):
        try:
            pl.read_ndjson(b"\n".join(half), **_READ_KWARGS)
        except _PARSE_ERRORS:
            bad |= {i + offset for i in _bisect_bad_lines(half)}
    return bad


def parse_basic_ac_db(gz_bytes: bytes, max_drop_rate: float):
    total = 0
    rejected = 0

    # Streamed: decompress and validate line by line, writing only accepted+projected bytes to
    # disk — no raw decompressed blob, no lines list, no in-memory join on the healthy path.
    with tempfile.NamedTemporaryFile(suffix=".ndjson") as tf:
        gz_io = io.BytesIO(gz_bytes)
        del gz_bytes
        with gzip.GzipFile(fileobj=gz_io) as gz:
            for raw_line in gz:
                line = raw_line.rstrip(b"\n")
                total += 1
                # A line is valid only if it decodes to a JSON object — `null`/numbers/arrays parse
                # fine but aren't records, and would reach polars as a silent all-null row.
                try:
                    obj = json.loads(line, parse_constant=_reject_constant)
                except (ValueError, json.JSONDecodeError):
                    rejected += 1
                    continue
                if not isinstance(obj, dict):
                    rejected += 1
                    continue
                # Combination-only conflicts (e.g. faa_pia true vs [] in two records) are invisible
                # to bisection below — each half parses alone — so this per-record gate is the only catch.
                if _type_violation(obj):
                    rejected += 1
                    continue
                tf.write(_project(obj))
                tf.write(b"\n")

        # Ceiling checked before the expensive polars parse, not after — an over-rate file is
        # rejected for the cost of a JSON scan, never a full read_ndjson.
        _drop_rate_check(rejected, total, max_drop_rate)
        kept = total - rejected
        tf.flush()

        # Explicit schema over the now-uniform, contract-validated projection: inference is gone
        # entirely, so the combination-SchemaError class documented above cannot reach here.
        try:
            df = pl.read_ndjson(tf.name, **_READ_KWARGS)
        except _PARSE_ERRORS:
            # A projected-but-still-polars-incompatible line (e.g. an unpaired UTF-16 surrogate)
            # survived the type contract; bisect the accepted lines to charge it, not abort the batch.
            tf.seek(0)
            accepted = tf.read().split(b"\n")
            if accepted and accepted[-1] == b"":
                accepted.pop()
            bad_indices = _bisect_bad_lines(accepted)
            rejected += len(bad_indices)
            _drop_rate_check(rejected, total, max_drop_rate)
            survivors = [ln for i, ln in enumerate(accepted) if i not in bad_indices]
            kept = len(survivors)
            df = pl.read_ndjson(b"\n".join(survivors), **_READ_KWARGS)

    # polars silently disagreeing with our own line accounting is a failure to surface, never absorb.
    if df.height != kept:
        raise ValueError(f"basic-ac-db parsed height {df.height} != kept line count {kept} — accounting mismatch")

    return df, total, rejected
