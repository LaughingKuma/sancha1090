from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pendulum

from airflow.sdk import dag, task

from include.adsb_assets import adsb_bronze_table, adsb_raw_landed


SNAPSHOT_RETENTION = timedelta(days=7)

log = logging.getLogger("tableize_adsb")


def tableize_core(catalog, engine) -> dict:
    """Commit pending adsb_state Parquet to Iceberg via zero-copy add_files (byte-mirror bronze).
    Safe to replay: add_files_to_adsb reconciles paths a prior crashed run already committed."""
    from include import adsb_iceberg as ai
    from include import adsb_manifest as am

    table = ai.ensure_adsb_namespace_and_table(catalog)

    pending = am.pending_adsb_uris(engine)
    if not pending:
        log.info("nothing pending")
        return {"committed": 0, "files": 0}

    uris = [p["s3_uri"] for p in pending]
    snapshot_by_uri = ai.add_files_to_adsb(table, uris)
    snapshot_by_filename = {p["filename"]: snapshot_by_uri[p["s3_uri"]] for p in pending}
    committed = am.mark_iceberg_committed(snapshot_by_filename, engine)

    _expire_snapshots(table)
    return {"committed": committed, "files": len(pending)}


def _expire_snapshots(table) -> None:
    # Non-fatal per the retry matrix: metadata bloat is benign for hours; next run retries.
    try:
        threshold = datetime.now(timezone.utc) - SNAPSHOT_RETENTION
        table.maintenance.expire_snapshots().older_than(threshold).commit()
    except Exception:
        log.exception("expire_snapshots failed (non-fatal)")


@dag(
    dag_id="tableize_adsb",
    description="Add newly-landed adsb_state Parquet into Iceberg bronze.adsb_states via add_files",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=[adsb_raw_landed],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "bronze", "iceberg", "adsb"],
)
def tableize_adsb():

    @task(outlets=[adsb_bronze_table])
    def add_pending_to_iceberg() -> dict:
        from include import adsb_iceberg as ai
        from include import adsb_manifest as am

        return tableize_core(ai.get_catalog(), am._engine())

    add_pending_to_iceberg()


tableize_adsb()
