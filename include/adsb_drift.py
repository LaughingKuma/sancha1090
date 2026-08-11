from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable, Iterator

import pyarrow as pa

from include.adsb_schema import (
    DOUBLE_FIELDS,
    INT_FIELDS,
    JSON_FIELDS,
    LIST_FIELDS,
    STRING_FIELDS,
)


# The "typed readsb fields" view (the buckets track capture_v2); capture_ts/_raw_json/_schema_version
# are ours, not readsb keys, so they're correctly absent.
TYPED_READSB_FIELDS = set(
    STRING_FIELDS + DOUBLE_FIELDS + INT_FIELDS + LIST_FIELDS + JSON_FIELDS
)

# Untyped readsb keys already triaged in week-1 manual scans — left raw-only on purpose, so they
# must not re-trigger. The alert fires only on keys outside both the typed set and this allowlist.
KNOWN_UNTYPED = {"dbFlags", "calc_track"}


def find_new_untyped_fields(observed_keys: set[str], known_untyped: set[str]) -> set[str]:
    """Keys seen in _raw_json that are neither typed in the bronze schema nor on the known-untyped
    allowlist — i.e. genuinely new readsb fields that warrant a promotion decision."""
    return observed_keys - TYPED_READSB_FIELDS - known_untyped


def count_raw_json_keys(
    batches: Iterable[pa.RecordBatch], sample_rows: int
) -> tuple[Counter, int]:
    """Tally top-level keys across `_raw_json` rows, stopping after `sample_rows` parsed dicts.
    Null and unparseable rows are skipped (don't count toward the cap)."""
    seen: Counter = Counter()
    parsed = 0
    for batch in batches:
        for raw in batch.column("_raw_json").to_pylist():
            if parsed >= sample_rows:
                return seen, parsed
            if raw is None:
                continue
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obj, dict):
                parsed += 1
                seen.update(obj.keys())
    return seen, parsed


# Bound the scan: newest files + a row cap keep it memory-bounded (mirrors the operator script).
DEFAULT_LIMIT_FILES = 48
DEFAULT_SAMPLE_ROWS = 200_000

log = logging.getLogger("maintain_adsb_schema")


def _iter_raw_json_batches(fs, paths: list[str]) -> Iterator:
    import pyarrow.parquet as pq

    for p in paths:
        with fs.open_input_file(p) as f:
            yield from pq.ParquetFile(f).iter_batches(columns=["_raw_json"])


# A new _raw_json key means the edge producer's schema moved: bronze DDL + adsb_schema must follow in
# lock-step, so non-empty drift log.errors (the alert) instead of silently landing untyped data.
def scan_core(fs, root: str, *, limit_files: int, sample_rows: int, log=log) -> dict:
    from pyarrow.fs import FileSelector

    infos = fs.get_file_info(FileSelector(root, recursive=True, allow_not_found=True))
    paths = sorted(i.path for i in infos if i.path.endswith(".parquet"))
    if limit_files:
        paths = paths[-limit_files:]

    seen, parsed = count_raw_json_keys(_iter_raw_json_batches(fs, paths), sample_rows)
    new_fields = find_new_untyped_fields(set(seen), KNOWN_UNTYPED)
    suppressed = sorted(set(seen) & KNOWN_UNTYPED)

    summary = {
        "files": len(paths),
        "rows_parsed": parsed,
        "distinct_keys": len(seen),
        "new_fields": sorted(new_fields),
        "suppressed": suppressed,
    }
    if new_fields:
        log.error("adsb schema drift: %d new untyped readsb field(s) in _raw_json: %s — decide "
                  "promote/raw-only/silver per field", len(new_fields), summary["new_fields"])
    else:
        log.info("adsb schema drift scan clean: %s", summary)
    return summary
