from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


# Bound the scan: newest files + a row cap keep it memory-bounded (mirrors the operator script).
DEFAULT_LIMIT_FILES = 48
DEFAULT_SAMPLE_ROWS = 200_000

log = logging.getLogger("maintain_adsb_schema")


def _iter_raw_json_batches(fs, paths: list[str]) -> Iterator:
    import pyarrow.parquet as pq

    for p in paths:
        with fs.open_input_file(p) as f:
            yield from pq.ParquetFile(f).iter_batches(columns=["_raw_json"])


def scan_core(fs, root: str, *, limit_files: int, sample_rows: int, log=log) -> dict:
    """Scan the most-recent Parquet under `root`, tally `_raw_json` top-level keys, and diff against
    the bronze typed set + known-untyped allowlist. Non-empty drift → log.error (the alert)."""
    from pyarrow.fs import FileSelector

    from include import adsb_drift as dr

    infos = fs.get_file_info(FileSelector(root, recursive=True, allow_not_found=True))
    paths = sorted(i.path for i in infos if i.path.endswith(".parquet"))
    if limit_files:
        paths = paths[-limit_files:]

    seen, parsed = dr.count_raw_json_keys(_iter_raw_json_batches(fs, paths), sample_rows)
    new_fields = dr.find_new_untyped_fields(set(seen), dr.KNOWN_UNTYPED)
    suppressed = sorted(set(seen) & dr.KNOWN_UNTYPED)

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


@dag(
    dag_id="maintain_adsb_schema",
    description="Weekly scan of bronze _raw_json for new untyped readsb fields (schema-drift alert)",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule="35 4 * * 1",  # Monday 04:35 UTC, off-peak weekly slot
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["sancha1090", "bronze", "adsb", "maintenance"],
)
def maintain_adsb_schema():

    @task
    def scan_drift() -> dict:
        from include.s3_helpers import garage_pyarrow_fs, get_bucket

        return scan_core(garage_pyarrow_fs(), f"{get_bucket()}/bronze/adsb_state",
                         limit_files=DEFAULT_LIMIT_FILES, sample_rows=DEFAULT_SAMPLE_ROWS)

    scan_drift()


maintain_adsb_schema()
