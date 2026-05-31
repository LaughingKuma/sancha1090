from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable

import pyarrow as pa

from include.adsb_iceberg import (
    _DOUBLE_FIELDS,
    _INT_FIELDS,
    _JSON_FIELDS,
    _LIST_FIELDS,
    _STRING_FIELDS,
)


# Mirrors the producer's typed set: the schema-match test guarantees these lists track capture_v2,
# so this IS the "typed readsb fields" view. capture_ts/_raw_json/_schema_version are ours, not
# readsb keys, and live outside these lists — so they're correctly absent.
TYPED_READSB_FIELDS = set(
    _STRING_FIELDS + _DOUBLE_FIELDS + _INT_FIELDS + _LIST_FIELDS + _JSON_FIELDS
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
