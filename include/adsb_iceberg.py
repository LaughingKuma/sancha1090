from __future__ import annotations

from typing import Optional

from pyiceberg.catalog import Catalog
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.types import (
    DoubleType,
    IntegerType,
    ListType,
    LongType,
    NestedField,
    StringType,
)

from include.iceberg_rest import get_polaris_catalog


NAMESPACE = "bronze"
TABLE = "adsb_states"
QUALIFIED = f"{NAMESPACE}.{TABLE}"


# Field buckets are kept in lock-step with capture_v2._build_schema() (STRING/DOUBLE/INT/
# LIST/JSON_FIELDS). test_producer_parquet_matches_table_schema fails on any drift.
_STRING_FIELDS = ["hex", "type", "r", "t", "desc", "category", "sil_type",
                  "emergency", "ownOp", "year", "flight", "squawk", "alt_baro"]
_DOUBLE_FIELDS = ["now", "lat", "lon", "r_dst", "r_dir", "seen", "seen_pos", "rssi",
                  "gs", "mach", "track", "track_rate", "roll", "mag_heading",
                  "true_heading", "nav_qnh", "nav_heading"]
_INT_FIELDS = ["messages", "nic", "rc", "version", "nac_p", "nac_v", "sil", "nic_baro",
               "gva", "sda", "alert", "spi", "alt_geom", "ias", "tas", "baro_rate",
               "geom_rate", "nav_altitude_mcp", "nav_altitude_fms", "wd", "ws", "oat", "tat"]
_LIST_FIELDS = ["nav_modes", "mlat", "tisb"]
_JSON_FIELDS = ["acas_ra"]


def _build_schema() -> Schema:
    # Column order mirrors capture_v2._build_schema() — the producer contract add_files maps by name.
    cols: list[tuple[str, object]] = [("capture_ts", DoubleType())]
    cols += [(n, StringType()) for n in _STRING_FIELDS]
    cols += [(n, DoubleType()) for n in _DOUBLE_FIELDS]
    cols += [(n, LongType()) for n in _INT_FIELDS]
    cols += [(n, "list_string") for n in _LIST_FIELDS]
    cols += [(n, StringType()) for n in _JSON_FIELDS]
    cols += [("_raw_json", StringType()), ("_schema_version", IntegerType())]

    fields: list[NestedField] = []
    # List element ids live at 100+ so they never collide with the positional top-level ids.
    # All fields nullable: capture_v2 declares every column nullable and byte-mirror add_files
    # cannot promote a nullable Parquet column to a required Iceberg one. capture_ts/_schema_version
    # are invariants at the DATA level (always set), not the column level — enforcing non-null is a
    # producer-side _schema_version bump (v4.x).
    for i, (name, ftype) in enumerate(cols, start=1):
        if ftype == "list_string":
            ftype = ListType(element_id=100 + i, element_type=StringType(), element_required=False)
        fields.append(NestedField(i, name, ftype, required=False))
    return Schema(*fields)


ADSB_SCHEMA = _build_schema()

# Unpartitioned: capture_ts is a Double and Iceberg DayTransform rejects non-temporal
# source types; file-level Parquet min/max on capture_ts prune well at this volume.
PARTITION_SPEC = UNPARTITIONED_PARTITION_SPEC

TABLE_PROPERTIES = {
    "write.format.default": "parquet",
    "write.parquet.compression-codec": "zstd",
    "write.metadata.compression-codec": "gzip",
    "format-version": "2",
    "history.expire.max-snapshot-age-ms": "604800000",  # 7 days, matching the per-run expire_snapshots floor
}

# ADDED status in the Iceberg manifest-entry metadata table (0=EXISTING, 1=ADDED, 2=DELETED).
_ENTRY_STATUS_ADDED = 1


def ensure_adsb_namespace_and_table(catalog: Optional[Catalog] = None) -> Table:
    """Idempotent. The bronze namespace already exists in Polaris (v2.1 bootstrap); the
    if_not_exists calls make this a no-op on re-run."""
    cat = catalog or get_polaris_catalog()
    cat.create_namespace_if_not_exists(NAMESPACE)
    return cat.create_table_if_not_exists(
        QUALIFIED,
        schema=ADSB_SCHEMA,
        partition_spec=PARTITION_SPEC,
        properties=TABLE_PROPERTIES,
    )


def _norm(path: str) -> str:
    # Iceberg stores local paths as file:///abs; normalize so a bare /abs input matches.
    return path[len("file://"):] if path.startswith("file://") else path


def _current_data_file_paths(table: Table) -> set[str]:
    files = table.inspect.data_files()
    return {_norm(p) for p in files.column("file_path").to_pylist()}


def _paths_added_in_snapshot(table: Table, snapshot_id: int) -> set[str]:
    entries = table.inspect.entries(snapshot_id=snapshot_id)
    statuses = entries.column("status").to_pylist()
    data_files = entries.column("data_file").to_pylist()
    return {
        _norm(df["file_path"])
        for status, df in zip(statuses, data_files, strict=True)
        if status == _ENTRY_STATUS_ADDED
    }


def add_files_to_adsb(table: Table, paths: list[str]) -> dict[str, int]:
    """Register producer Parquet in-place (zero-copy). Returns {input_path: snapshot_id} for
    every input path. Idempotent under 'add_files succeeded but mark_committed crashed' replay:
    paths already present are reconciled to the snapshot that first added them."""
    existing = _current_data_file_paths(table)
    new_paths = [p for p in paths if _norm(p) not in existing]
    snapshot_by_path: dict[str, int] = {}

    if new_paths:
        table.add_files(new_paths, check_duplicate_files=True)
        new_snap = table.current_snapshot().snapshot_id
        for p in new_paths:
            snapshot_by_path[p] = new_snap

    # Recovery: these paths were committed by a prior run that crashed before mark_committed —
    # reattribute each to its original snapshot so the caller can still mark them.
    already_present = {p for p in paths if _norm(p) in existing}
    if already_present:
        for snap in reversed(table.history()):
            added = _paths_added_in_snapshot(table, snap.snapshot_id)
            for p in list(already_present):
                if _norm(p) in added:
                    snapshot_by_path[p] = snap.snapshot_id
                    already_present.discard(p)
            if not already_present:
                break

    # A path present in the table's data files but added by no retained snapshot means its adding
    # snapshot was expired (added before the 7-day retention floor). Fail loudly rather than return
    # a dict the caller would index into with a KeyError.
    if already_present:
        raise RuntimeError(
            f"add_files_to_adsb({QUALIFIED}): cannot attribute already-present path(s) to any "
            f"retained snapshot (adding snapshot expired past the 7-day window): "
            f"{sorted(_norm(p) for p in already_present)}"
        )

    return snapshot_by_path
